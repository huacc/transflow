"""执行 G8 资源、Catalog、证据、反过拟合和最终 PDF 的正式校验。"""

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
from transflow.toolboxes.leaves import build_p8_toolbox_factories

LOGGER = logging.getLogger("scripts.verify_p8")
REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v3.json"
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FINGERPRINT_PATH = REPO_ROOT / "resources" / "manifests" / "p8_resource_fingerprints.json"
EVIDENCE_ROOT = REPO_ROOT / "resources" / "evidence" / "p8"
MIGRATION_ROOT = REPO_ROOT / "docs" / "迁移"
OUTPUT_PDF = REPO_ROOT / "output" / "pdf" / "P8_first_batch_mixed_final.pdf"
OUTPUT_SUMMARY = REPO_ROOT / "output" / "pdf" / "P8_first_batch_mixed_summary.json"
LEAF_ROOT = REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves"
ROUTES = (
    "visual_only",
    "body.flow_text.single",
    "body.chart",
    "body.diagram",
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
    """从固定迁移记录恢复一个叶证据并重新执行哈希校验。"""

    name = route.replace(".", "_")
    payload = json.loads((MIGRATION_ROOT / f"p8_{name}_migration.json").read_text(encoding="utf-8"))
    return LeafMigrationEvidence.from_dict(payload)


def verify_resources() -> tuple[int, tuple[str, ...]]:
    """复核 P8 指纹清单中每个资源仍存在且内容未漂移。"""

    payload = json.loads(FINGERPRINT_PATH.read_text(encoding="utf-8"))
    failures: list[str] = []
    for item in payload["resources"]:
        path = REPO_ROOT / str(item["path"])
        if not path.is_file() or _sha256_file(path) != item["sha256"]:
            failures.append(str(item["path"]))
    return len(payload["resources"]), tuple(failures)


def scan_leaf_sources() -> tuple[int, tuple[str, ...]]:
    """扫描生产叶源码，拒绝绝对路径、身份分支和直连 Provider。"""

    files = tuple(sorted(LEAF_ROOT.glob("*.py")))
    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for code, pattern in FORBIDDEN_SOURCE_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{code}:{path.name}")
    return len(files), tuple(sorted(violations))


def verify_gate() -> dict[str, Any]:
    """汇总 G8 六项正式检查并在任一硬条件失败时返回非 PASS。"""

    LOGGER.info("调用 G8 正式校验，意图=复核叶证明、Catalog 和完整 PDF")
    factories = build_p8_toolbox_factories(POLICY_PATH, FONT_MANIFEST, REPO_ROOT)
    catalog = load_toolbox_catalog(CATALOG_PATH, factories)
    startup = catalog.validate_startup()
    evidence = tuple(_load_evidence(route) for route in ROUTES)
    attestations = LeafGateEvaluator().evaluate_all(evidence)
    entry_by_route = {entry.route: entry for entry in catalog.entries}
    publication_errors: list[str] = []
    for attestation in attestations.attestations:
        try:
            validate_catalog_publication(entry_by_route[attestation.route], attestation)
        except Exception as error:
            publication_errors.append(f"{attestation.route}:{type(error).__name__}")
    resource_count, resource_failures = verify_resources()
    source_file_count, source_violations = scan_leaf_sources()
    runtime_summary = json.loads(OUTPUT_SUMMARY.read_text(encoding="utf-8"))
    pdf_readable = False
    pdf_page_count = 0
    if OUTPUT_PDF.is_file() and runtime_summary["output_hash"] == _sha256_file(OUTPUT_PDF):
        with pymupdf.open(OUTPUT_PDF) as document:
            pdf_page_count = document.page_count
            pdf_readable = document.page_count == 4
    conclusion_by_route = {item.route: item.conclusion.value for item in attestations.attestations}
    enabled_routes = tuple(sorted(entry.route for entry in catalog.entries if entry.enabled))
    required_enabled = {"visual_only", "body.flow_text.single"}
    required_disabled = {"body.chart", "body.diagram"}
    leaf_states_valid = (
        required_enabled <= set(enabled_routes)
        and all(not entry_by_route[route].enabled for route in required_disabled)
        and conclusion_by_route["visual_only"] == "PASS_ENABLE"
        and conclusion_by_route["body.flow_text.single"] == "PASS_ENABLE"
        and conclusion_by_route["body.chart"] == "PASS_DISABLED_WITH_FALLBACK"
        and conclusion_by_route["body.diagram"] == "PASS_DISABLED_WITH_FALLBACK"
    )
    page_summaries = runtime_summary["pages"]
    mixed_valid = (
        runtime_summary["preservation_passed"] is True
        and runtime_summary["page_count"] == 4
        and tuple(item["page_no"] for item in page_summaries) == (1, 2, 3, 4)
        and page_summaries[2]["fallback"] == "PAGE_PASSTHROUGH"
        and page_summaries[3]["fallback"] == "PAGE_PASSTHROUGH"
    )
    checks = {
        "catalog_startup": startup.ready,
        "four_leaf_publication_consistency": not publication_errors,
        "leaf_states": leaf_states_valid,
        "mixed_pdf": mixed_valid and pdf_readable,
        "resource_fingerprints": not resource_failures,
        "source_boundary_scan": not source_violations,
    }
    return {
        "gate": "G8",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "catalog_hash": catalog.catalog_hash,
        "enabled_routes": enabled_routes,
        "conclusions": conclusion_by_route,
        "publication_errors": publication_errors,
        "resource_count": resource_count,
        "resource_failures": resource_failures,
        "source_file_count": source_file_count,
        "source_violations": source_violations,
        "output_pdf": OUTPUT_PDF.relative_to(REPO_ROOT).as_posix(),
        "output_hash": _sha256_file(OUTPUT_PDF) if OUTPUT_PDF.is_file() else None,
        "output_page_count": pdf_page_count,
        "preservation_passed": runtime_summary["preservation_passed"],
    }


def main() -> int:
    """打印机器可读 G8 结果，并以进程退出码表达 Gate 结论。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    result = verify_gate()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
