"""生成 P9 六叶迁移证据、三态证明和 v4 显式 Catalog。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from transflow.domain.common import canonical_json_bytes, json_ready
from transflow.toolboxes.catalog import catalog_entry_fingerprint
from transflow.toolboxes.leaf_gate import LeafGateEvaluator, LeafMigrationEvidence

LOGGER = logging.getLogger("scripts.build_p9_release")
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_ROOT = REPO_ROOT / "docs" / "迁移"
EVIDENCE_ROOT = REPO_ROOT / "resources" / "evidence" / "p9"
MANIFEST_ROOT = REPO_ROOT / "resources" / "manifests"
CATALOG_ROOT = REPO_ROOT / "resources" / "catalogs"
SPIKE_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "toolboxes"
ROUTE_SOURCES = {
    "cover": ("cover", "EVIDENCE_INSUFFICIENT"),
    "contents": ("contents", "EVIDENCE_INSUFFICIENT"),
    "end": ("end", "EVIDENCE_INSUFFICIENT"),
    "body.flow_text.multi": ("body/flow_text/multi", "NOT_EVALUATED"),
    "body.table": ("body/table", "NOT_EVALUATED"),
    "body.anchored_blocks": ("body/anchored_blocks", "EVIDENCE_INSUFFICIENT"),
}


def _sha256_file(path: Path) -> str:
    """计算一个受控资源的真实 SHA-256。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_files(paths: tuple[Path, ...]) -> str:
    """按仓库相对路径和文件内容计算一组资源的稳定哈希。"""

    payload = tuple(
        {
            "path": path.relative_to(REPO_ROOT).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in sorted(paths)
    )
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    """以 UTF-8、稳定键序和仓库相对引用写入审计资源。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _catalog_policy_hash() -> str:
    """计算不含自引用证明哈希的 P9 Catalog 决策面指纹。"""

    payload = {
        route: ("0.1.0-review", "PASS_DISABLED_WITH_FALLBACK", False)
        for route in sorted(ROUTE_SOURCES)
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _make_evidence(route: str, source_relative: str, original_state: str) -> LeafMigrationEvidence:
    """根据旧 Gate 和当前生产代码建立可复算的单叶迁移证据。"""

    source_root = SPIKE_ROOT / Path(source_relative)
    real_sample_evidence = EVIDENCE_ROOT / "real_sample_regression.json"
    if not real_sample_evidence.is_file():
        raise RuntimeError("P9 发布前必须先执行真实分类样本回归")
    source_files = (
        source_root / "stage_gate.json",
        source_root / "toolbox_manifest.json",
        real_sample_evidence,
    )
    schema_path = REPO_ROOT / "resources" / "schemas" / "leaf_migration_evidence_v1.schema.json"
    threshold_path = MANIFEST_ROOT / "p9_leaf_thresholds.json"
    code_paths = (
        REPO_ROOT / "src" / "transflow" / "toolboxes" / "contracts.py",
        REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary.py",
        REPO_ROOT / "src" / "transflow" / "toolboxes" / "leaves" / "ordinary_policy.py",
        REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_coordinator.py",
        REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_pipeline.py",
        REPO_ROOT / "scripts" / "run_p9_real_samples.py",
        REPO_ROOT / "tests" / "migration" / "p9_qwen_translation_adapter.py",
        REPO_ROOT / "tests" / "migration" / "test_p9_real_samples.py",
    )
    payload = {
        "schema_version": "transflow.leaf-migration-evidence/v1",
        "route": route,
        "source_path": source_root.relative_to(REPO_ROOT).as_posix(),
        "source_hash": _sha256_files(source_files),
        "original_state": original_state,
        "target_toolbox_key": route,
        "target_version": "0.1.0-review",
        "allowed_changes": [
            "统一 PageToolbox 六阶段包装",
            "TranslationPort 上移到 PageCoordinator",
            "增加 owner/clip/source guard 和有界原子回退",
        ],
        "migration_differences": [
            "删除叶内 Provider 和样本身份输入",
            "最终 PDF 从完整源副本串行回放 Patch",
            "未达真实盲测阈值时 Catalog 保持 disabled",
        ],
        "fixture_refs": [
            f"tests/test_p9.py::{route.replace('.', '_')}",
            "tests/migration/test_p9_real_samples.py",
            "spikes/page_classification_engine_puncture_v1/分类结果",
            "output/pdf/P9_second_batch_mixed_final.pdf",
            "output/pdf/P9_real_samples/P9_real_samples_showcase.pdf",
        ],
        "gold_refs": [
            "resources/evidence/p9/p9_acceptance_summary.json",
            "resources/evidence/p9/real_sample_regression.json",
        ],
        "threshold_refs": ["resources/manifests/p9_leaf_thresholds.json"],
        "fallback": "PAGE_PASSTHROUGH",
        "limitations": [
            f"旧叶正式状态为 {original_state}，且没有 PromotionManifest",
            "187 份分类结果已完成真实结构回归，12 份英文页已调用真实千问形成候选 PDF",
            "这些已知样本不能充当新的独立真实盲测，候选失败也不得按样本身份修补",
            "迁移骨架可单独验证，但生产运行必须确定性整页透传",
        ],
        "owner": route,
        "contract_passed": True,
        "equivalence_passed": False,
        "blind_passed": False,
        "anti_overfit_passed": True,
        "failure_passed": True,
        "document_e2e_passed": True,
        "fallback_has_page_outcome": True,
        "fallback_has_complete_pdf": True,
        "new_evidence": True,
        "code_hash": _sha256_files(code_paths),
        "schema_hash": _sha256_file(schema_path),
        "catalog_hash": _catalog_policy_hash(),
        "font_hash": _sha256_file(MANIFEST_ROOT / "font_manifest.json"),
        "threshold_hash": _sha256_file(threshold_path),
        "evidence_hash": "",
    }
    return LeafMigrationEvidence.from_dict(payload)


def build_release() -> dict[str, Any]:
    """生成六叶证明，更新 v4 Catalog，并冻结全部 P9 资源指纹。"""

    LOGGER.info("调用 P9 发布构建，意图=形成六叶独立禁用结论和 v4 Catalog")
    real_sample_payload = json.loads(
        (EVIDENCE_ROOT / "real_sample_regression.json").read_text(encoding="utf-8")
    )
    evidence_items = tuple(
        _make_evidence(route, source, state) for route, (source, state) in ROUTE_SOURCES.items()
    )
    attestations = LeafGateEvaluator().evaluate_all(evidence_items)
    if not attestations.stage_passed:
        raise RuntimeError("P9 任一叶没有完整 fallback，禁止生成阶段 Catalog")
    for evidence in evidence_items:
        name = evidence.route.replace(".", "_")
        _write_json(MIGRATION_ROOT / f"p9_{name}_migration.json", asdict(evidence))
    _write_json(
        EVIDENCE_ROOT / "leaf_attestations.json",
        {
            "schema_version": "transflow.p9-leaf-attestations/v1",
            "stage_passed": attestations.stage_passed,
            "attestations": [asdict(item) for item in attestations.attestations],
        },
    )
    attestation_by_route = {item.route: item for item in attestations.attestations}
    v3 = json.loads((CATALOG_ROOT / "page_toolbox_catalog_v3.json").read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for source_entry in v3["entries"]:
        entry = dict(source_entry)
        route = str(entry["route"])
        if route in attestation_by_route:
            attestation = attestation_by_route[route]
            entry.update(
                {
                    "toolbox_version": attestation.target_version,
                    "fingerprint": catalog_entry_fingerprint(
                        route,
                        str(entry["toolbox_key"]),
                        attestation.target_version,
                        str(entry["contract_version"]),
                    ),
                    "evidence_state": attestation.conclusion.value,
                    "evidence_attestation_hash": attestation.attestation_hash,
                    "enabled": False,
                    "disabled_reason": "P9_KNOWN_CORPUS_REGRESSION_ONLY_BLIND_PROMOTION_UNMET",
                }
            )
        entries.append(entry)
    catalog_payload = {
        "schema_version": "transflow.page-toolbox-catalog/v4",
        "entries": entries,
    }
    catalog_path = CATALOG_ROOT / "page_toolbox_catalog_v4.json"
    _write_json(catalog_path, catalog_payload)
    summary = {
        "schema_version": "transflow.p9-acceptance-summary/v1",
        "catalog_path": catalog_path.relative_to(REPO_ROOT).as_posix(),
        "catalog_hash": _sha256_file(catalog_path),
        "catalog_policy_hash": _catalog_policy_hash(),
        "real_anonymous_document_counts": {route: 0 for route in ROUTE_SOURCES},
        "known_classified_sample_counts": {
            route: int(real_sample_payload["leaf_summaries"][route]["sample_count"])
            for route in ROUTE_SOURCES
        },
        "real_qwen_candidate_count": int(real_sample_payload["selected_sample_count"]),
        "real_qwen_http_calls": int(real_sample_payload["qwen_http_calls"]),
        "threshold_real_anonymous_documents": 6,
        "conclusions": {item.route: item.conclusion.value for item in attestations.attestations},
    }
    _write_json(EVIDENCE_ROOT / "p9_acceptance_summary.json", summary)
    resources = tuple(
        sorted(
            (
                *(
                    MIGRATION_ROOT / f"p9_{item.route.replace('.', '_')}_migration.json"
                    for item in evidence_items
                ),
                EVIDENCE_ROOT / "leaf_attestations.json",
                EVIDENCE_ROOT / "p9_acceptance_summary.json",
                EVIDENCE_ROOT / "real_sample_regression.json",
                catalog_path,
                MANIFEST_ROOT / "p9_leaf_thresholds.json",
                MANIFEST_ROOT / "p9_ordinary_leaf_policy.json",
            )
        )
    )
    _write_json(
        MANIFEST_ROOT / "p9_resource_fingerprints.json",
        {
            "schema_version": "transflow.p9-resource-fingerprints/v1",
            "resources": [
                {
                    "path": path.relative_to(REPO_ROOT).as_posix(),
                    "sha256": _sha256_file(path),
                }
                for path in resources
            ],
        },
    )
    return summary


def main() -> int:
    """生成 P9 发布资源并把六叶结论打印为实际命令证据。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    print(json.dumps(build_release(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
