"""使用隔离真实模型适配器计算 P5 匿名逐叶质量与风险指标。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from scripts.build_p5_baseline import (
    ANSWER_KEY_PATH,
    BASELINE_PATH,
    RECEIPT_PATH,
    THRESHOLD_PATH,
    load_json,
    locate_authorized_pdf,
    sha256_file,
)
from tests.migration.qwen_adapter import (
    MigrationQwenDecisionAdapter,
    migration_environment_ready,
)
from transflow.classification.baseline import FrozenThresholdRegistry
from transflow.classification.decision_adapter import BoundedDecisionRunner
from transflow.classification.engine import ClassificationEngine
from transflow.pdf_kernel.facts import PageFactsExtractor

LOGGER = logging.getLogger("transflow.tests.migration.p5_quality")
TESTS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS_ROOT.parent
METRICS_PATH = REPO_ROOT / "docs" / "reports" / "gates" / "P5_classification_metrics.json"


def _route_role(route: str) -> str:
    """返回用于高代价误路由判断的一级角色。"""

    return route.split(".", maxsplit=1)[0]


def _metric_payload(
    predictions: list[dict[str, str]],
    actual_model_call_count: int,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """计算逐叶 precision/recall、freeform 率和高代价误路由率。"""

    leaf_metrics: dict[str, dict[str, Any]] = {}
    gate_failures: list[str] = []
    for leaf, threshold in thresholds["leaf_thresholds"].items():
        expected_count = sum(item["expected_route"] == leaf for item in predictions)
        predicted_count = sum(item["predicted_route"] == leaf for item in predictions)
        true_positive = sum(
            item["expected_route"] == leaf and item["predicted_route"] == leaf
            for item in predictions
        )
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / expected_count if expected_count else 0.0
        passed = True
        if threshold["evidence_status"] == "SUPPORTED":
            passed = (
                expected_count >= threshold["minimum_samples"]
                and precision >= threshold["precision_min"]
                and recall >= threshold["recall_min"]
            )
            if not passed:
                gate_failures.append(f"leaf:{leaf}")
        leaf_metrics[leaf] = {
            "evidence_status": threshold["evidence_status"],
            "expected_count": expected_count,
            "precision": round(precision, 6),
            "predicted_count": predicted_count,
            "recall": round(recall, 6),
            "threshold_pass": passed,
            "true_positive": true_positive,
        }
    total = len(predictions)
    freeform_rate = sum(item["predicted_route"] == "body.freeform" for item in predictions) / total
    high_cost_count = sum(
        _route_role(item["expected_route"]) != _route_role(item["predicted_route"])
        for item in predictions
    )
    high_cost_rate = high_cost_count / total
    if freeform_rate > thresholds["global_thresholds"]["freeform_rate_max"]:
        gate_failures.append("global:freeform_rate")
    if high_cost_rate > thresholds["global_thresholds"]["high_cost_misroute_rate_max"]:
        gate_failures.append("global:high_cost_misroute_rate")
    route_coverage = sum(bool(item["predicted_route"]) for item in predictions) / total
    if route_coverage < thresholds["global_thresholds"]["model_failure_route_coverage_min"]:
        gate_failures.append("global:route_coverage")
    return {
        "schema_version": "transflow.classification-quality-metrics/v1",
        "actual_model_call_count": actual_model_call_count,
        "anonymous_case_count": total,
        "fake_quality_result_count": 0,
        "freeform_rate": round(freeform_rate, 6),
        "gate_failures": gate_failures,
        "high_cost_misroute_count": high_cost_count,
        "high_cost_misroute_rate": round(high_cost_rate, 6),
        "leaf_metrics": leaf_metrics,
        "quality_gate_pass": not gate_failures and actual_model_call_count > 0,
        "route_coverage": round(route_coverage, 6),
    }


def _write_metrics(payload: dict[str, Any]) -> None:
    """原子写入不含模型原文和秘密的质量 Gate 证据。"""

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = METRICS_PATH.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(METRICS_PATH)


@pytest.mark.migration
@pytest.mark.skipif(not migration_environment_ready(), reason="真实迁移模型环境变量未配置")
def test_p5_5_t03_real_qwen_anonymous_quality_meets_frozen_thresholds() -> None:
    """P5.5-T03：真实模型盲测计入逐叶指标且必须达到事前冻结阈值。"""

    baseline = load_json(BASELINE_PATH)
    answer_key = {
        str(item["case_key"]): str(item["route"]) for item in load_json(ANSWER_KEY_PATH)["answers"]
    }
    threshold_registry = FrozenThresholdRegistry.load(THRESHOLD_PATH, RECEIPT_PATH)
    adapter = MigrationQwenDecisionAdapter()
    engine = ClassificationEngine(BoundedDecisionRunner(adapter))
    predictions: list[dict[str, str]] = []
    for case in baseline["cases"]:
        source = locate_authorized_pdf(str(case["content_sha256"]))
        facts = PageFactsExtractor().extract_all(
            source,
            sha256_file(source),
            include_classification=True,
        )[0]
        result = engine.classify_page(facts, 1)
        predictions.append(
            {
                "case_key": str(case["case_key"]),
                "expected_route": answer_key[str(case["case_key"])],
                "predicted_route": result.route.route,
            }
        )
        LOGGER.info(
            "匿名质量样本完成，意图=累计真实指标 progress=%s/%s",
            len(predictions),
            len(baseline["cases"]),
        )
    metrics = _metric_payload(
        predictions,
        adapter.call_count,
        threshold_registry.payload,
    )
    metrics["anonymous_baseline_sha256"] = sha256_file(BASELINE_PATH)
    metrics["sealed_answer_key_sha256"] = sha256_file(ANSWER_KEY_PATH)
    metrics["threshold_registry_sha256"] = threshold_registry.file_sha256
    metrics["predictions"] = predictions
    _write_metrics(metrics)
    assert metrics["fake_quality_result_count"] == 0
    assert metrics["quality_gate_pass"] is True, metrics["gate_failures"]


def main() -> int:
    """只输出真实迁移模型环境是否就绪，不执行或伪造质量结果。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(f"P5_REAL_QUALITY_ENV ready={migration_environment_ready()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
