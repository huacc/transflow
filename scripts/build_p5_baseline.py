"""核对 P5 匿名分类 fixture、真实 PDF 哈希和冻结收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from transflow.classification.decision_adapter import find_identity_leaks

LOGGER = logging.getLogger("transflow.p5.baseline")
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_ROOT = REPO_ROOT / "resources" / "manifests"
BASELINE_PATH = MANIFEST_ROOT / "p5_anonymous_baseline.json"
RECEIPT_PATH = MANIFEST_ROOT / "p5_threshold_freeze_receipt.json"
THRESHOLD_PATH = MANIFEST_ROOT / "p5_classification_thresholds.json"
ANSWER_KEY_PATH = REPO_ROOT / "tests" / "migration" / "classification_answer_key.json"
AUTHORIZED_PDF_ROOTS = (REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "样本1",)


def sha256_file(path: Path) -> str:
    """流式计算真实 PDF 或冻结资源的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    """读取必须是 JSON 对象的仓库相对资源。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象:{path.name}")
    return value


def stable_baseline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """按 case_key 稳定重建匿名集，用于证明重复生成无漂移。"""

    return {
        "schema_version": payload["schema_version"],
        "fixture_revision": payload["fixture_revision"],
        "cases": sorted(payload["cases"], key=lambda item: item["case_key"]),
    }


def baseline_content_hash(payload: dict[str, Any]) -> str:
    """计算与 JSON 排版无关的匿名基线内容哈希。"""

    encoded = json.dumps(
        stable_baseline_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def locate_authorized_pdf(content_sha256: str) -> Path:
    """只在授权样本根中按内容哈希定位唯一真实 PDF，不依赖文件名。"""

    matches = [
        path
        for root in AUTHORIZED_PDF_ROOTS
        for path in sorted(root.glob("*.pdf"))
        if sha256_file(path) == content_sha256
    ]
    if not matches:
        raise ValueError(f"匿名内容哈希未命中授权 PDF:{content_sha256[:12]}")
    # 多个文件名若拥有同一内容哈希，任取其一不会改变匿名输入或评测结果。
    return matches[0]


def verify_all() -> dict[str, Any]:
    """逐项核对无泄漏、哈希唯一、答案分离和事前冻结不变量。"""

    LOGGER.info("调用匿名基线核对，意图=验证 P5.1 可重放且未泄漏身份")
    baseline = load_json(BASELINE_PATH)
    answer_key = load_json(ANSWER_KEY_PATH)
    receipt = load_json(RECEIPT_PATH)
    cases = stable_baseline_payload(baseline)["cases"]
    if find_identity_leaks(baseline):
        raise ValueError("匿名 fixture 存在身份或答案泄漏")
    case_keys = [str(item["case_key"]) for item in cases]
    content_hashes = [str(item["content_sha256"]) for item in cases]
    if len(case_keys) != len(set(case_keys)) or len(content_hashes) != len(set(content_hashes)):
        raise ValueError("匿名 fixture 含重复 case 或内容哈希")
    answer_keys = [str(item["case_key"]) for item in answer_key["answers"]]
    if set(answer_keys) != set(case_keys) or len(answer_keys) != len(set(answer_keys)):
        raise ValueError("测试答案与匿名 fixture 不能一一对应")
    located = 0
    for case in cases:
        path = locate_authorized_pdf(str(case["content_sha256"]))
        if path.stat().st_size != int(case["byte_count"]):
            raise ValueError("匿名 fixture 的真实 PDF 字节数漂移")
        located += 1
    if receipt["anonymous_baseline_sha256"] != sha256_file(BASELINE_PATH):
        raise ValueError("匿名 fixture 与冻结收据不一致")
    if receipt["sealed_answer_key_sha256"] != sha256_file(ANSWER_KEY_PATH):
        raise ValueError("测试答案在冻结后发生变化")
    if receipt["threshold_registry_sha256"] != sha256_file(THRESHOLD_PATH):
        raise ValueError("阈值注册表在冻结后发生变化")
    return {
        "anonymous_case_count": len(cases),
        "baseline_content_sha256": baseline_content_hash(baseline),
        "identity_leak_count": 0,
        "located_real_pdf_count": located,
        "post_freeze_change_count": 0,
        "stratum_count": len({str(item["stratum_key"]) for item in cases}),
    }


def parse_args() -> argparse.Namespace:
    """解析只读核对命令，P5 不提供结果后改写 fixture 的入口。"""

    parser = argparse.ArgumentParser(description="核对 P5 匿名分类基线")
    parser.add_argument("--check", action="store_true", help="执行完整只读核对")
    return parser.parse_args()


def main() -> int:
    """执行匿名基线核对并输出可进入 Gate 报告的真实摘要。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parse_args()
    summary = verify_all()
    print(
        "P5_ANONYMOUS_BASELINE PASS " + " ".join(f"{key}={value}" for key, value in summary.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
