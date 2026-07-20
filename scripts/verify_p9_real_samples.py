"""验证 P9 真实分类样本、千问候选 PDF 和禁用发布证据。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pymupdf

from transflow.toolboxes.catalog import load_toolbox_catalog

LOGGER = logging.getLogger("transflow.scripts.verify_p9_real_samples")
REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_PATH = REPO_ROOT / "resources" / "evidence" / "p9" / "real_sample_regression.json"
SUMMARY_PATH = REPO_ROOT / "output" / "pdf" / "P9_real_samples" / "P9_real_samples_summary.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
EXPECTED_COUNTS = {
    "cover": 60,
    "contents": 34,
    "end": 20,
    "body.flow_text.multi": 23,
    "body.table": 20,
    "body.anchored_blocks": 30,
}


def _sha256_file(path: Path) -> str:
    """流式复算真实输入和候选产物内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """读取一个 UTF-8 JSON 对象并拒绝非对象根。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根不是对象:{path.name}")
    return value


def verify() -> dict[str, Any]:
    """逐项复验 187 份来源、12 份候选、预览、Catalog 和证据一致性。"""

    LOGGER.info("调用 P9 真实样本 Gate，意图=复验输入哈希、候选 PDF 和禁用纪律")
    evidence = _load_json(EVIDENCE_PATH)
    summary = _load_json(SUMMARY_PATH)
    checks: dict[str, bool] = {
        "evidence_summary_identical": evidence == summary,
        "schema_version": evidence.get("schema_version")
        == "transflow.p9-real-sample-regression/v2",
        "total_sample_count": evidence.get("total_sample_count") == sum(EXPECTED_COUNTS.values()),
        "selected_sample_count": evidence.get("selected_sample_count") == 12,
        "real_qwen_called": int(evidence.get("qwen_http_calls", 0)) >= 12,
        "blind_promotion_not_claimed": evidence.get("blind_promotion_eligible") is False,
        "catalog_decision_disabled": evidence.get("catalog_decision")
        == "PASS_DISABLED_WITH_FALLBACK",
    }
    leaf_summaries = evidence.get("leaf_summaries", {})
    checks["leaf_counts"] = all(
        leaf_summaries.get(route, {}).get("sample_count") == count
        for route, count in EXPECTED_COUNTS.items()
    )
    records = evidence.get("sample_records", [])
    source_failures: list[str] = []
    for item in records:
        path = REPO_ROOT / str(item["relative_path"])
        if not path.is_file() or _sha256_file(path) != item["source_hash"]:
            source_failures.append(str(item["relative_path"]))
    checks["all_source_hashes"] = not source_failures and len(records) == 187

    candidate_failures: list[str] = []
    accepted_candidates = 0
    fallback_candidates = 0
    diagnostic_candidates = 0
    for item in evidence.get("candidate_results", []):
        candidate = REPO_ROOT / str(item["candidate_path"])
        preview = REPO_ROOT / str(item["preview_path"])
        production_safe = REPO_ROOT / str(item["production_safe_path"])
        production_preview = REPO_ROOT / str(item["production_preview_path"])
        source = REPO_ROOT / str(item["relative_path"])
        try:
            if _sha256_file(candidate) != item["candidate_hash"]:
                raise ValueError("候选哈希漂移")
            with pymupdf.open(candidate) as document:
                if document.page_count != 1:
                    raise ValueError("候选页数漂移")
                if (
                    document.metadata.get("subject")
                    != "UNSAFE DIAGNOSTIC CANDIDATE - NOT FOR PRODUCTION"
                ):
                    raise ValueError("诊断候选缺少不可发布标识")
            pixmap = pymupdf.Pixmap(preview.read_bytes())
            if pixmap.width < 1 or pixmap.height < 1:
                raise ValueError("预览尺寸非法")
            if item["patch_operations"] < 1 or _sha256_file(candidate) == _sha256_file(source):
                raise ValueError("诊断候选没有写入原始提案")
            if item["diagnostic_render_status"] != "WRITTEN_UNSAFE_DIAGNOSTIC":
                raise ValueError("诊断候选未成功写入")
            if _sha256_file(production_safe) != item["production_safe_hash"]:
                raise ValueError("生产安全结果哈希漂移")
            production_pixmap = pymupdf.Pixmap(production_preview.read_bytes())
            if production_pixmap.width < 1 or production_pixmap.height < 1:
                raise ValueError("生产安全预览尺寸非法")
            if item["approved_patch_operations"] == 0 and _sha256_file(
                production_safe
            ) != _sha256_file(source):
                raise ValueError("生产回退结果未保持源字节")
            if float(item["production_outside_declared_region_diff_ratio"]) != 0.0:
                raise ValueError("生产批准区域外发生像素变化")
            for evidence_key in (
                "translation_request_path",
                "translation_bundle_path",
                "layout_plan_path",
                "quality_judgement_path",
            ):
                _load_json(REPO_ROOT / str(item[evidence_key]))
        except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
            candidate_failures.append(f"{item.get('route')}:{type(error).__name__}")
        if item.get("diagnostic_render_status") == "WRITTEN_UNSAFE_DIAGNOSTIC":
            diagnostic_candidates += 1
        if item.get("verdict") == "ACCEPT" and int(item.get("approved_patch_operations", 0)) > 0:
            accepted_candidates += 1
        if item.get("verdict") == "FALLBACK" and int(item.get("approved_patch_operations", 0)) == 0:
            fallback_candidates += 1
    checks["candidate_artifacts"] = not candidate_failures
    checks["diagnostic_candidate_for_every_selected_sample"] = diagnostic_candidates == 12
    checks["fallback_keeps_diagnostic_candidate"] = (
        evidence.get("fallback_with_diagnostic_candidate") == fallback_candidates
    )
    checks["accepted_candidate_exists"] = accepted_candidates >= 1
    checks["fallback_candidate_exists"] = fallback_candidates >= 1

    catalog = load_toolbox_catalog(CATALOG_PATH)
    catalog_states = {
        route: (
            resolution.entry.enabled if resolution.entry is not None else None,
            resolution.finding.code if resolution.finding is not None else None,
        )
        for route in EXPECTED_COUNTS
        for resolution in (catalog.resolve_enabled(route, 1),)
    }
    checks["all_six_catalog_entries_disabled"] = all(
        state == (False, "TOOLBOX_DISABLED") for state in catalog_states.values()
    )
    status = "PASS" if all(checks.values()) else "FAIL"
    return {
        "accepted_candidates": accepted_candidates,
        "candidate_failures": candidate_failures,
        "checks": checks,
        "diagnostic_candidates": diagnostic_candidates,
        "fallback_candidates": fallback_candidates,
        "qwen_http_calls": evidence.get("qwen_http_calls"),
        "selected_sample_count": evidence.get("selected_sample_count"),
        "source_failures": source_failures,
        "status": status,
        "total_sample_count": evidence.get("total_sample_count"),
    }


def main() -> int:
    """打印机器可读 Gate 结果，并以退出码表达通过或失败。"""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
