"""逐项验证 P2 合同、Catalog、Schema、Port 和评审闭环。"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.verify_architecture import DEFAULT_SOURCE_ROOT, scan_production_tree
from transflow.ports import (
    ArtifactPort,
    CheckpointPort,
    JobQueuePort,
    ModelDecisionPort,
    TranslationPort,
)

LOGGER = logging.getLogger("transflow.p2.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "docs" / "迁移" / "migration_ledger.json"
TAXONOMY_PATH = REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v1.json"
RESOURCE_MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "p2_resource_fingerprints.json"
REVIEW_PATH = REPO_ROOT / "docs" / "合同" / "P2领域合同与架构评审_v0.1.md"
GOVERNANCE_PATH = REPO_ROOT / "docs" / "迁移" / "governance_registry.json"
EXPECTED_PORTS = {
    "ArtifactPort": ArtifactPort,
    "CheckpointPort": CheckpointPort,
    "JobQueuePort": JobQueuePort,
    "ModelDecisionPort": ModelDecisionPort,
    "TranslationPort": TranslationPort,
}
EXPECTED_METHODS: dict[str, frozenset[str]] = {
    "ArtifactPort": frozenset({"get", "put"}),
    "CheckpointPort": frozenset({"load", "save"}),
    "JobQueuePort": frozenset({"acquire", "publish_result", "read_control"}),
    "ModelDecisionPort": frozenset({"decide"}),
    "TranslationPort": frozenset({"translate"}),
}


def load_json(path: Path) -> dict[str, Any]:
    """读取一个仓库内 UTF-8 JSON 对象。"""

    return json.loads(path.read_text(encoding="utf-8"))


def catalog_violations(
    taxonomy: dict[str, Any],
    catalog: dict[str, Any],
) -> list[str]:
    """返回路由缺失、重复、额外或无晋升证据启用等 Catalog 违规。"""

    expected_routes = [item["route"] for item in taxonomy["routes"]]
    entries = catalog["entries"]
    actual_routes = [item["route"] for item in entries]
    violations: list[str] = []
    if len(actual_routes) != len(set(actual_routes)):
        violations.append("DUPLICATE_ROUTE")
    if set(expected_routes) - set(actual_routes):
        violations.append("MISSING_ROUTE")
    if set(actual_routes) - set(expected_routes):
        violations.append("EXTRA_ROUTE")
    for entry in entries:
        if not entry.get("evidence_refs"):
            violations.append(f"MISSING_EVIDENCE:{entry['route']}")
        if entry.get("enabled") and (
            entry.get("evidence_status") != "PASS"
            or entry.get("promotion_manifest_present") is not True
        ):
            violations.append(f"UNVERIFIED_ENABLED:{entry['route']}")
    return violations


def verify_contracts() -> list[str]:
    """验证领域与 Port 包可导入且未引入外部实现模块。"""

    violations = scan_production_tree(DEFAULT_SOURCE_ROOT)
    return [f"{item.code}:{item.relative_path}:{item.line}" for item in violations]


def verify_ports() -> list[str]:
    """验证公开 Port 恰好五个、名称无重复且方法集合完全冻结。"""

    violations: list[str] = []
    actual_names = {
        name
        for name, value in vars(__import__("transflow.ports", fromlist=["*"])).items()
        if name.endswith("Port") and inspect.isclass(value)
    }
    if actual_names != set(EXPECTED_PORTS):
        violations.append(
            f"PORT_SET expected={sorted(EXPECTED_PORTS)} actual={sorted(actual_names)}"
        )
    for port_name, methods in EXPECTED_METHODS.items():
        port = EXPECTED_PORTS[port_name]
        actual_methods = {
            name
            for name, value in vars(port).items()
            if callable(value) and not name.startswith("_")
        }
        if actual_methods != methods:
            violations.append(
                "PORT_METHODS "
                f"port={port_name} expected={sorted(methods)} actual={sorted(actual_methods)}"
            )
    if len({id(port) for port in EXPECTED_PORTS.values()}) != 5:
        violations.append("DUPLICATE_PORT_CLASS")
    return violations


def verify_catalog() -> list[str]:
    """验证设计路由、版本化 Taxonomy 与初始 Catalog 完全一致。"""

    ledger = load_json(LEDGER_PATH)
    taxonomy = load_json(TAXONOMY_PATH)
    catalog = load_json(CATALOG_PATH)
    routes = [item["route"] for item in taxonomy["routes"]]
    violations = catalog_violations(taxonomy, catalog)
    if routes != ledger["route_behavior_keys"]:
        violations.append("TAXONOMY_LEDGER_ORDER_MISMATCH")
    if taxonomy.get("schema_version") != "transflow.page-route-taxonomy/v1":
        violations.append("TAXONOMY_VERSION")
    if catalog.get("schema_version") != "transflow.page-toolbox-catalog/v1":
        violations.append("CATALOG_VERSION")
    return violations


def verify_schemas() -> list[str]:
    """验证两份 Schema 及其他 P2 资源的版本和冻结哈希未漂移。"""

    manifest = load_json(RESOURCE_MANIFEST_PATH)
    violations: list[str] = []
    if manifest.get("schema_version") != "transflow.p2-resource-fingerprints/v1":
        violations.append("RESOURCE_MANIFEST_VERSION")
    resources = manifest.get("resources", [])
    paths = [item["path"] for item in resources]
    if len(paths) != len(set(paths)) or len(paths) != 4:
        violations.append("RESOURCE_MANIFEST_MEMBERS")
    schema_versions: set[str] = set()
    for item in resources:
        path = REPO_ROOT / item["path"]
        if not path.is_file():
            violations.append(f"RESOURCE_MISSING:{item['path']}")
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != item["sha256"]:
            violations.append(f"RESOURCE_HASH:{item['path']}")
        if item["path"].endswith(".schema.json"):
            schema_versions.add(item["schema_version"])
    expected_versions = {"transflow.translation-bundle/v1", "transflow.model-decision/v1"}
    if schema_versions != expected_versions:
        violations.append(f"SCHEMA_VERSIONS:{sorted(schema_versions)}")
    return violations


def verify_review() -> list[str]:
    """验证架构评审已明确通过且没有遗留边界问题或开放决策。"""

    violations: list[str] = []
    review = REVIEW_PATH.read_text(encoding="utf-8") if REVIEW_PATH.is_file() else ""
    if "评审结论：PASS" not in review:
        violations.append("REVIEW_NOT_PASS")
    if "开放问题：0" not in review:
        violations.append("REVIEW_OPEN_ISSUES")
    governance = load_json(GOVERNANCE_PATH)
    current_stage = governance.get("current_stage", {})
    if current_stage.get("open_decision_ids"):
        violations.append("OPEN_BOUNDARY_DECISION")
    return violations


CHECKS: dict[str, Callable[[], list[str]]] = {
    "contracts": verify_contracts,
    "ports": verify_ports,
    "catalog": verify_catalog,
    "schemas": verify_schemas,
    "review": verify_review,
}


def execute(selected: str) -> int:
    """执行一个或全部 P2 检查，并打印逐项可追溯结论。"""

    check_names = tuple(CHECKS) if selected == "all" else (selected,)
    failed = False
    for check_name in check_names:
        LOGGER.info("调用 P2 验收检查，意图=验证阶段合同 check=%s", check_name)
        violations = CHECKS[check_name]()
        status = "PASS" if not violations else "FAIL"
        print(f"P2_VERIFY check={check_name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P2_VERIFY_VIOLATION check={check_name} detail={violation}")
        failed = failed or bool(violations)
    print(f"P2_VERIFY_SUMMARY status={'FAIL' if failed else 'PASS'} checks={len(check_names)}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    """解析 P2 检查分组。"""

    parser = argparse.ArgumentParser(description="验证 Transflow P2 合同与架构边界")
    parser.add_argument("check", choices=("all", *CHECKS), nargs="?", default="all")
    return parser.parse_args()


def main() -> int:
    """执行命令行指定的 P2 验收检查。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    return execute(parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
