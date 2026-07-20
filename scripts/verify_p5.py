"""核验 P5 匿名基线、迁移边界、文档接线和真实质量证据。"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable
from pathlib import Path

from scripts.build_p5_baseline import (
    BASELINE_PATH,
    RECEIPT_PATH,
    THRESHOLD_PATH,
    load_json,
    sha256_file,
    verify_all,
)
from scripts.verify_architecture import DEFAULT_SOURCE_ROOT, scan_production_tree

LOGGER = logging.getLogger("transflow.p5.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFICATION_ROOT = REPO_ROOT / "src" / "transflow" / "classification"
PROMPT_SOURCE_ROOT = (
    REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "prompts"
)
PROMPT_TARGET_ROOT = REPO_ROOT / "resources" / "prompts" / "classification"
COORDINATOR_PATH = REPO_ROOT / "src" / "transflow" / "application" / "document_coordinator.py"
METRICS_PATH = REPO_ROOT / "docs" / "reports" / "gates" / "P5_classification_metrics.json"


def baseline_violations() -> list[str]:
    """核对匿名 fixture、真实 PDF、分层和冻结哈希。"""

    try:
        summary = verify_all()
    except (KeyError, OSError, TypeError, ValueError) as error:
        return [f"BASELINE_INVALID:{type(error).__name__}"]
    violations: list[str] = []
    if summary["identity_leak_count"] != 0:
        violations.append("BASELINE_IDENTITY_LEAK")
    if summary["anonymous_case_count"] != summary["located_real_pdf_count"]:
        violations.append("BASELINE_REAL_PDF_COVERAGE")
    if summary["post_freeze_change_count"] != 0:
        violations.append("BASELINE_POST_FREEZE_CHANGE")
    return violations


def prompt_violations() -> list[str]:
    """确认八份生产 Prompt 与来源文本在统一换行后完全一致。"""

    violations: list[str] = []
    source_files = tuple(sorted(PROMPT_SOURCE_ROOT.rglob("*.md")))
    target_files = tuple(sorted(PROMPT_TARGET_ROOT.rglob("*.md")))
    source_relatives = {path.relative_to(PROMPT_SOURCE_ROOT) for path in source_files}
    target_relatives = {path.relative_to(PROMPT_TARGET_ROOT) for path in target_files}
    if source_relatives != target_relatives or len(source_files) != 8:
        violations.append("PROMPT_FILE_SET_DRIFT")
        return violations
    for relative in sorted(source_relatives):
        source = (
            (PROMPT_SOURCE_ROOT / relative)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .rstrip()
        )
        target = (
            (PROMPT_TARGET_ROOT / relative)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .rstrip()
        )
        if source != target:
            violations.append(f"PROMPT_CONTENT_DRIFT:{relative.as_posix()}")
    return violations


def production_boundary_violations() -> list[str]:
    """扫描生产分类包，禁止模型直连、固定 Route 和样本运行时依赖。"""

    violations: list[str] = []
    forbidden_patterns = {
        "DIRECT_PROVIDER_NAME": re.compile(r"qwen", re.IGNORECASE),
        "DIRECT_CHAT_ENDPOINT": re.compile(r"/chat/completions", re.IGNORECASE),
        "DIRECT_HTTP_CLIENT": re.compile(r"\bhttpx\b", re.IGNORECASE),
        "FIXED_ROUTE": re.compile(r"FixedRoute|fixed_route", re.IGNORECASE),
        "SAMPLE_RUNTIME": re.compile(r"sample_dir|source_manifest", re.IGNORECASE),
    }
    for path in sorted(CLASSIFICATION_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        for code, pattern in forbidden_patterns.items():
            if pattern.search(text):
                violations.append(f"{code}:{relative}")
    if COORDINATOR_PATH.is_file():
        coordinator = COORDINATOR_PATH.read_text(encoding="utf-8")
        for required in ("run_classified", "classify_pages", "include_classification=True"):
            if required not in coordinator:
                violations.append(f"COORDINATOR_WIRING_MISSING:{required}")
    else:
        violations.append("COORDINATOR_MISSING")
    return sorted(violations)


def architecture_violations() -> list[str]:
    """复用 P2 架构扫描，验证 P5 引入的 classification 依赖方向。"""

    return [
        f"{item.code}:{item.relative_path}:{item.line}"
        for item in scan_production_tree(DEFAULT_SOURCE_ROOT)
    ]


def quality_violations() -> list[str]:
    """核对真实模型指标文件、三份冻结哈希和 fake 质量计数。"""

    if not METRICS_PATH.is_file():
        return ["REAL_QUALITY_METRICS_MISSING"]
    metrics = load_json(METRICS_PATH)
    receipt = load_json(RECEIPT_PATH)
    violations: list[str] = []
    expected_hashes = {
        "anonymous_baseline_sha256": sha256_file(BASELINE_PATH),
        "sealed_answer_key_sha256": receipt["sealed_answer_key_sha256"],
        "threshold_registry_sha256": sha256_file(THRESHOLD_PATH),
    }
    for key, expected in expected_hashes.items():
        if metrics.get(key) != expected:
            violations.append(f"QUALITY_HASH_MISMATCH:{key}")
    if metrics.get("fake_quality_result_count") != 0:
        violations.append("FAKE_RESULT_COUNT_NONZERO")
    if not isinstance(metrics.get("actual_model_call_count"), int) or metrics.get(
        "actual_model_call_count",
        0,
    ) <= 0:
        violations.append("REAL_MODEL_CALL_COUNT_ZERO")
    if metrics.get("quality_gate_pass") is not True:
        violations.append("QUALITY_THRESHOLD_NOT_MET")
    if metrics.get("route_coverage") != 1.0:
        violations.append("QUALITY_ROUTE_COVERAGE")
    return violations


def migration_adapter_violations() -> list[str]:
    """确认直接模型和 httpx 只位于 tests/migration 隔离目录。"""

    production_hits: list[str] = []
    pattern = re.compile(r"qwen|/chat/completions|PAGE_CLASSIFIER_QWEN", re.IGNORECASE)
    for path in sorted(DEFAULT_SOURCE_ROOT.rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            production_hits.append(path.relative_to(REPO_ROOT).as_posix())
    migration_adapter = REPO_ROOT / "tests" / "migration" / "qwen_adapter.py"
    if not migration_adapter.is_file() or "httpx" not in migration_adapter.read_text(
        encoding="utf-8"
    ):
        production_hits.append("MIGRATION_ADAPTER_MISSING")
    return production_hits


def all_checks() -> dict[str, list[str]]:
    """执行 P5 可独立复算的全部静态与真实质量检查。"""

    return {
        "baseline": baseline_violations(),
        "prompts": prompt_violations(),
        "production": production_boundary_violations(),
        "architecture": architecture_violations(),
        "migration_adapter": migration_adapter_violations(),
        "quality": quality_violations(),
    }


CHECKS: dict[str, Callable[[], list[str]]] = {
    "baseline": baseline_violations,
    "prompts": prompt_violations,
    "production": production_boundary_violations,
    "architecture": architecture_violations,
    "migration_adapter": migration_adapter_violations,
    "quality": quality_violations,
}


def parse_args() -> argparse.Namespace:
    """解析 P5 核验范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P5 页面分类迁移")
    parser.add_argument("check", choices=("all", *CHECKS))
    return parser.parse_args()


def main() -> int:
    """执行选定核验并输出适合 Gate 保存的逐项结果。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    selected = parse_args().check
    results = all_checks() if selected == "all" else {selected: CHECKS[selected]()}
    for name, violations in results.items():
        status = "PASS" if not violations else "FAIL"
        print(f"P5_VERIFY check={name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P5_VIOLATION check={name} detail={violation}")
    passed = not any(results.values())
    print(f"P5_VERIFY_SUMMARY status={'PASS' if passed else 'FAIL'} checks={len(results)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
