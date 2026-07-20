"""执行 G9 资源、Catalog、六叶证据、边界和混合 PDF 正式校验。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import pymupdf

from transflow.toolboxes.catalog import load_toolbox_catalog
from transflow.toolboxes.leaf_gate import (
    LeafGateEvaluator,
    LeafMigrationEvidence,
    validate_catalog_publication,
)
from transflow.toolboxes.leaves import build_p9_toolbox_factories

LOGGER = logging.getLogger("scripts.verify_p9")
REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FINGERPRINT_PATH = REPO_ROOT / "resources" / "manifests" / "p9_resource_fingerprints.json"
MIGRATION_ROOT = REPO_ROOT / "docs" / "迁移"
OUTPUT_PDF = REPO_ROOT / "output" / "pdf" / "P9_second_batch_mixed_final.pdf"
OUTPUT_SUMMARY = REPO_ROOT / "output" / "pdf" / "P9_second_batch_mixed_summary.json"
REAL_SAMPLE_EVIDENCE = REPO_ROOT / "resources" / "evidence" / "p9" / "real_sample_regression.json"
SOURCE_FILES = (
    REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py",
    REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary_policy.py",
    REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "factory.py",
    REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_coordinator.py",
    REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_pipeline.py",
)
P9_ROUTES = (
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)
FORBIDDEN_SOURCE_PATTERNS = {
    "ABSOLUTE_DRIVE_LITERAL": re.compile(r"[A-Za-z]:[\\/]"),
    "SAMPLE_ID_BRANCH": re.compile(r"\bsample_id\b", re.IGNORECASE),
    "FILENAME_BRANCH": re.compile(r"\bfile_?name\b", re.IGNORECASE),
    "DIRECT_HTTP": re.compile(r"\b(?:httpx|requests|urllib3)\b"),
    "DIRECT_PROVIDER": re.compile(r"\bprovider_client\b"),
}


def _sha256_file(path: Path) -> str:
    """计算正式 Gate 引用文件的真实 SHA-256。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_evidence(route: str) -> LeafMigrationEvidence:
    """从固定迁移记录恢复一个 P9 叶证据并复算内容哈希。"""

    payload = json.loads(
        (MIGRATION_ROOT / f"p9_{route.replace('.', '_')}_migration.json").read_text(
            encoding="utf-8"
        )
    )
    return LeafMigrationEvidence.from_dict(payload)


def verify_resources() -> tuple[int, tuple[str, ...]]:
    """复核 P9 指纹清单中的每个资源仍存在且内容未漂移。"""

    payload = json.loads(FINGERPRINT_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    for item in payload["resources"]:
        path = REPO_ROOT / str(item["path"])
        if not path.is_file() or _sha256_file(path) != item["sha256"]:
            failures.append(str(item["path"]))
    return len(payload["resources"]), tuple(failures)


def scan_sources() -> tuple[int, tuple[str, ...]]:
    """扫描 P9 生产源码，拒绝绝对路径、身份分支和直连 Provider。"""

    violations: list[str] = []
    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for code, pattern in FORBIDDEN_SOURCE_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{code}:{path.relative_to(REPO_ROOT).as_posix()}")
    return len(SOURCE_FILES), tuple(sorted(violations))


def verify_gate() -> dict[str, Any]:
    """汇总 G9 八项硬检查并在任一条件失败时返回 FAIL。"""

    LOGGER.info("调用 G9 正式校验，意图=复核六叶结论、Catalog 和完整 PDF")
    factories = build_p9_toolbox_factories(
        P8_POLICY,
        P9_POLICY,
        FONT_MANIFEST,
        REPO_ROOT,
    )
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    evidence = tuple(_load_evidence(route) for route in P9_ROUTES)
    attestations = LeafGateEvaluator().evaluate_all(evidence)
    entry_by_route = {entry.route: entry for entry in catalog.entries}
    publication_errors: list[str] = []
    for attestation in attestations.attestations:
        try:
            validate_catalog_publication(entry_by_route[attestation.route], attestation)
        except Exception as error:
            publication_errors.append(f"{attestation.route}:{type(error).__name__}")
    resource_count, resource_failures = verify_resources()
    source_file_count, source_violations = scan_sources()
    runtime = json.loads(OUTPUT_SUMMARY.read_text(encoding="utf-8"))
    real_samples = json.loads(REAL_SAMPLE_EVIDENCE.read_text(encoding="utf-8"))
    pdf_readable = False
    pdf_page_count = 0
    if OUTPUT_PDF.is_file() and runtime["output_hash"] == _sha256_file(OUTPUT_PDF):
        with pymupdf.open(OUTPUT_PDF) as document:
            pdf_page_count = document.page_count
            pdf_readable = document.page_count == 10
    conclusions = {item.route: item.conclusion.value for item in attestations.attestations}
    enabled_routes = tuple(sorted(entry.route for entry in catalog.entries if entry.enabled))
    p9_disabled = all(
        not entry_by_route[route].enabled
        and entry_by_route[route].evidence_state == "PASS_DISABLED_WITH_FALLBACK"
        and conclusions[route] == "PASS_DISABLED_WITH_FALLBACK"
        for route in P9_ROUTES
    )
    pages = runtime["pages"]
    second_batch_pages = pages[4:]
    mixed_valid = (
        runtime["preservation_passed"] is True
        and runtime["page_count"] == 10
        and tuple(item["page_no"] for item in pages) == tuple(range(1, 11))
        and all(item["fallback"] == "PAGE_PASSTHROUGH" for item in second_batch_pages)
        and all(item["patch_operations"] == 0 for item in second_batch_pages)
        and runtime["contents_link_target_zero_based"] == 6
    )
    g8_regression = (
        pages[0]["route"] == "visual_only"
        and pages[0]["toolbox_version"] == "1.0.0"
        and pages[1]["route"] == "body.flow_text.single"
        and pages[1]["patch_operations"] == 1
        and pages[2]["route"] == "body.chart"
        and pages[2]["fallback"] == "PAGE_PASSTHROUGH"
        and pages[3]["route"] == "body.diagram"
        and pages[3]["fallback"] == "PAGE_PASSTHROUGH"
    )
    checks = {
        "catalog_startup": startup.ready,
        "six_leaf_publication_consistency": not publication_errors,
        "g9_1_cover": p9_disabled and conclusions["cover"] == "PASS_DISABLED_WITH_FALLBACK",
        "g9_2_contents": p9_disabled and runtime["contents_link_target_zero_based"] == 6,
        "g9_3_end": p9_disabled and conclusions["end"] == "PASS_DISABLED_WITH_FALLBACK",
        "g9_4_multi": p9_disabled
        and conclusions["body.flow_text.multi"] == "PASS_DISABLED_WITH_FALLBACK",
        "g9_5_table": p9_disabled and conclusions["body.table"] == "PASS_DISABLED_WITH_FALLBACK",
        "g9_6_anchored": p9_disabled
        and conclusions["body.anchored_blocks"] == "PASS_DISABLED_WITH_FALLBACK",
        "g9_7_enable_discipline": p9_disabled and not set(enabled_routes) & set(P9_ROUTES),
        "g9_8_mixed_and_g8_regression": mixed_valid and g8_regression and pdf_readable,
        "real_classified_sample_regression": real_samples["total_sample_count"] == 187
        and real_samples["selected_sample_count"] == 12
        and real_samples["qwen_http_calls"] >= 12
        and real_samples["blind_promotion_eligible"] is False
        and real_samples["catalog_decision"] == "PASS_DISABLED_WITH_FALLBACK",
        "resource_fingerprints": not resource_failures,
        "source_boundary_scan": not source_violations,
    }
    return {
        "gate": "G9",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "catalog_hash": catalog.catalog_hash,
        "enabled_routes": enabled_routes,
        "conclusions": conclusions,
        "publication_errors": publication_errors,
        "resource_count": resource_count,
        "resource_failures": resource_failures,
        "source_file_count": source_file_count,
        "source_violations": source_violations,
        "output_pdf": OUTPUT_PDF.relative_to(REPO_ROOT).as_posix(),
        "output_hash": _sha256_file(OUTPUT_PDF) if OUTPUT_PDF.is_file() else None,
        "output_page_count": pdf_page_count,
        "preservation_passed": runtime["preservation_passed"],
        "contents_link_target_zero_based": runtime["contents_link_target_zero_based"],
        "real_sample_count": real_samples["total_sample_count"],
        "real_qwen_candidate_count": real_samples["selected_sample_count"],
        "real_qwen_http_calls": real_samples["qwen_http_calls"],
    }


def main() -> int:
    """打印机器可读 G9 结果，并用退出码表达正式 Gate 结论。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    result = verify_gate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
