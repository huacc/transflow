"""复核 P7 六阶段、翻译边界、显式 Catalog、Margin、叶 Gate 和评审。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger("transflow.p7.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLBOX_ROOT = REPO_ROOT / "src" / "transflow" / "toolboxes"
CORE_ROOT = REPO_ROOT / "src" / "transflow" / "core"
COORDINATOR_PATH = REPO_ROOT / "src" / "transflow" / "application" / "toolbox_page_coordinator.py"
FINALIZER_PATH = REPO_ROOT / "src" / "transflow" / "application" / "document_finalizer.py"
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v2.json"
TAXONOMY_PATH = REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json"
STATE_PATH = REPO_ROOT / "docs" / "迁移" / "p7_leaf_initial_state.json"
TEMPLATE_PATH = REPO_ROOT / "docs" / "迁移" / "p7_leaf_migration_template.json"
MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "p7_resource_fingerprints.json"
TEST_PATH = REPO_ROOT / "tests" / "test_p7.py"
REVIEW_PATH = REPO_ROOT / "docs" / "评审" / "P7_Toolbox架构评审_v0.1.md"
FORBIDDEN_MODULE_ROOTS = {
    "sqlalchemy",
    "fastapi",
    "httpx",
    "requests",
    "litellm",
    "concurrent",
    "importlib",
    "merqfin",
    "pkgutil",
}
FORBIDDEN_TOOLBOX_IMPORTS = {
    "transflow.adapters",
    "transflow.application",
    "transflow.runtime",
    "transflow.ports",
}
FORBIDDEN_CALL_NAMES = {
    "entry_points",
    "iter_modules",
    "rglob",
    "glob",
    "register",
    "retry",
}


def _imports(tree: ast.AST) -> tuple[tuple[str, int], ...]:
    """提取 Python 文件中的显式 import 模块和行号。"""

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
    return tuple(found)


def contract_violations() -> list[str]:
    """核对 PageToolbox 恰好公开六个阶段且结果 DTO 齐全。"""

    path = TOOLBOX_ROOT / "contracts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    protocols = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PageToolbox"
    ]
    if len(protocols) != 1:
        return ["PAGE_TOOLBOX_PROTOCOL_COUNT"]
    method_names = tuple(
        node.name
        for node in protocols[0].body
        if isinstance(node, ast.FunctionDef) and node.name != "descriptor"
    )
    expected = (
        "prepare",
        "build_translation_request",
        "consume_translation_bundle",
        "render",
        "judge",
        "repair",
    )
    violations = [] if method_names == expected else [f"SIX_STAGE_MISMATCH:{method_names}"]
    content = path.read_text(encoding="utf-8")
    for symbol in (
        "ToolboxExecutionResult",
        "PagePatch",
        "Finding",
        "PageOutcome",
        "TranslationDispatch",
    ):
        if symbol not in content:
            violations.append(f"CONTRACT_SYMBOL_MISSING:{symbol}")
    return violations


def scan_toolbox_tree(root: Path) -> list[str]:
    """扫描指定 Python 树的直接外部依赖、动态发现和自调度越界。"""

    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=relative)
        for module, line in _imports(tree):
            if module.split(".", maxsplit=1)[0] in FORBIDDEN_MODULE_ROOTS:
                violations.append(f"FORBIDDEN_MODULE:{relative}:{line}:{module}")
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in FORBIDDEN_TOOLBOX_IMPORTS
            ):
                violations.append(f"TOOLBOX_LAYER_BYPASS:{relative}:{line}:{module}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and re.search(
                r"(?:provider|lease|database|merqfin|model.*selector|agent.*selector)",
                node.id,
                re.I,
            ):
                violations.append(
                    f"FORBIDDEN_RUNTIME_NAME:{relative}:{node.lineno}:{node.id}"
                )
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in FORBIDDEN_CALL_NAMES:
                violations.append(
                    f"DYNAMIC_OR_SELF_SCHEDULING:{relative}:{node.lineno}:{name}"
                )
        if re.search(r"https?://", text, re.I):
            violations.append(f"FORBIDDEN_RUNTIME_TOKEN:{relative}")
    return sorted(violations)


def architecture_violations() -> list[str]:
    """扫描 Toolbox/core，并核对翻译调度与最终化拒绝接缝。"""

    violations = scan_toolbox_tree(TOOLBOX_ROOT)
    if CORE_ROOT.is_dir():
        violations.extend(scan_toolbox_tree(CORE_ROOT))
    coordinator_text = COORDINATOR_PATH.read_text(encoding="utf-8")
    translation_seam_missing = (
        "TranslationPort" not in coordinator_text
        or "self._translation.translate" not in coordinator_text
    )
    if translation_seam_missing:
        violations.append("TRANSLATION_COORDINATOR_SEAM_MISSING")
    finalizer_text = FINALIZER_PATH.read_text(encoding="utf-8")
    finalizer_guard_missing = (
        "LEGACY_PAGE_SUFFIX" not in finalizer_text
        or "不得作为 DocumentFinalizer 输入" not in finalizer_text
    )
    if finalizer_guard_missing:
        violations.append("FINALIZER_LEGACY_REJECTION_MISSING")
    return sorted(violations)


def catalog_violations() -> list[str]:
    """核对设计 Route 唯一覆盖、静态禁用和确定 fallback。"""

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    entries = catalog.get("entries", [])
    routes = [str(item["route"]) for item in entries]
    expected = [str(item["route"]) for item in taxonomy["routes"]]
    violations: list[str] = []
    if catalog.get("schema_version") != "transflow.page-toolbox-catalog/v2":
        violations.append("CATALOG_SCHEMA_INVALID")
    if routes != expected or len(routes) != len(set(routes)):
        violations.append("CATALOG_ROUTE_COVERAGE_INVALID")
    for entry in entries:
        route = str(entry["route"])
        if entry.get("enabled") is not False:
            violations.append(f"P7_PREMATURE_ENABLE:{route}")
        if entry.get("fallback") != "PAGE_PASSTHROUGH":
            violations.append(f"FALLBACK_INVALID:{route}")
        if entry.get("evidence_attestation_hash") is not None:
            violations.append(f"UNVERIFIED_ATTESTATION:{route}")
    return violations


def leaf_state_violations() -> list[str]:
    """确认旧成熟度被原样导入且未升级，模板只允许三个结论。"""

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    leaves = state.get("leaves", [])
    if len(leaves) != 17:
        violations.append(f"LEAF_STATE_COUNT:{len(leaves)}")
    for leaf in leaves:
        if leaf.get("upgrade_performed") is not False or leaf.get("enabled") is not False:
            violations.append(f"LEAF_STATE_UPGRADED:{leaf.get('route')}")
    expected_conclusions = ["PASS_ENABLE", "PASS_DISABLED_WITH_FALLBACK", "FAIL"]
    if template.get("allowed_conclusions") != expected_conclusions:
        violations.append("LEAF_CONCLUSIONS_INVALID")
    required = template.get("required_fields", [])
    if len(required) != len(set(required)) or len(required) < 30:
        violations.append("LEAF_REQUIRED_FIELDS_INCOMPLETE")
    return violations


def resource_violations() -> list[str]:
    """复算 P7 静态资源 manifest 中登记的真实文件哈希。"""

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for item in payload.get("resources", []):
        path = REPO_ROOT / str(item["path"])
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual_hash != item.get("sha256"):
            violations.append(f"RESOURCE_DRIFT:{item['path']}")
    if len(payload.get("resources", [])) != 5:
        violations.append("RESOURCE_MANIFEST_COUNT")
    return violations


def test_inventory_violations() -> list[str]:
    """用 AST 核对 P7.1～P7.5 每个 T01～T06 恰有一个真实测试。"""

    if not TEST_PATH.is_file():
        return ["P7_TEST_FILE_MISSING"]
    tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"), filename=str(TEST_PATH))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_p7_")
    }
    violations: list[str] = []
    for stage in range(1, 6):
        for case in range(1, 7):
            prefix = f"test_p7_{stage}_t{case:02d}_"
            count = sum(name.startswith(prefix) for name in names)
            if count != 1:
                violations.append(f"TEST_ID_COUNT:{stage}.T{case:02d}:{count}")
    if len(names) != 30:
        violations.append(f"P7_TEST_TOTAL:{len(names)}")
    return violations


def review_violations() -> list[str]:
    """核对架构评审已有明确 PASS 且开放问题为零。"""

    if not REVIEW_PATH.is_file():
        return ["REVIEW_MISSING"]
    content = REVIEW_PATH.read_text(encoding="utf-8")
    violations = []
    if "评审结论：PASS" not in content:
        violations.append("REVIEW_NOT_PASS")
    if "开放问题：0" not in content:
        violations.append("REVIEW_OPEN_ISSUES")
    return violations


def all_checks() -> dict[str, list[str]]:
    """返回 G7 可复算的全部审计集合。"""

    return {
        "contract": contract_violations(),
        "architecture": architecture_violations(),
        "catalog": catalog_violations(),
        "leaf_state": leaf_state_violations(),
        "resources": resource_violations(),
        "test_inventory": test_inventory_violations(),
        "review": review_violations(),
    }


CHECKS: dict[str, Callable[[], list[str]]] = {
    "contract": contract_violations,
    "architecture": architecture_violations,
    "catalog": catalog_violations,
    "leaf_state": leaf_state_violations,
    "resources": resource_violations,
    "test_inventory": test_inventory_violations,
    "review": review_violations,
}


def parse_args() -> argparse.Namespace:
    """解析 P7 审计范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P7 Toolbox 迁移骨架")
    parser.add_argument("check", choices=("all", *CHECKS))
    return parser.parse_args()


def main() -> int:
    """执行选定审计并输出可直接进入 Gate 的稳定证据文本。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    selected = parse_args().check
    results = all_checks() if selected == "all" else {selected: CHECKS[selected]()}
    for name, violations in results.items():
        status = "PASS" if not violations else "FAIL"
        print(f"P7_VERIFY check={name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P7_VIOLATION check={name} detail={violation}")
    passed = not any(results.values())
    print(f"P7_VERIFY_SUMMARY status={'PASS' if passed else 'FAIL'} checks={len(results)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
