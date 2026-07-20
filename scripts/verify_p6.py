"""复核 P6 迁移来源、支持矩阵、机械边界、禁止调用和完整测试编号。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from transflow.pdf_kernel import build_kernel_fingerprint, load_support_matrix

LOGGER = logging.getLogger("transflow.p6.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "src" / "transflow" / "pdf_kernel"
MIGRATION_PATH = REPO_ROOT / "docs" / "迁移" / "p6_pdf_kernel_migration.json"
SUPPORT_MATRIX_PATH = REPO_ROOT / "resources" / "manifests" / "p6_preservation_support.json"
DETERMINISM_PATH = REPO_ROOT / "resources" / "manifests" / "p6_determinism_contract.json"
RUNTIME_BASELINE_PATH = REPO_ROOT / "resources" / "manifests" / "runtime_baseline.json"
FONT_MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
CONFIG_PATH = REPO_ROOT / "config" / "transflow.example.toml"
TEST_PATH = REPO_ROOT / "tests" / "test_p6.py"


def _sha256_file(path: Path) -> str:
    """流式计算迁移来源真实哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_violations() -> list[str]:
    """核对每个 spike 文件的来源哈希、生产目标和允许差异记录。"""

    payload = json.loads(MIGRATION_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    if payload.get("schema_version") != "transflow.p6-kernel-migration/v1":
        violations.append("MIGRATION_SCHEMA_INVALID")
    differences = payload.get("approved_differences")
    if not isinstance(differences, list) or not differences:
        violations.append("APPROVED_DIFFERENCES_MISSING")
    source_root = REPO_ROOT / str(payload.get("source_root", ""))
    target_root = REPO_ROOT / str(payload.get("target_root", ""))
    units = payload.get("units")
    if not isinstance(units, list) or len(units) != 11:
        return [*violations, "MIGRATION_UNIT_COVERAGE_INVALID"]
    for unit in units:
        source = source_root / str(unit["source"])
        target = target_root / str(unit["target"])
        if not source.is_file() or _sha256_file(source) != unit.get("source_sha256"):
            violations.append(f"MIGRATION_SOURCE_DRIFT:{unit['source']}")
        if not target.is_file():
            violations.append(f"MIGRATION_TARGET_MISSING:{unit['target']}")
    return sorted(violations)


def support_matrix_violations() -> list[str]:
    """核对每项承诺都有检测器、验证器、fixture 和合法处置。"""

    try:
        matrix = load_support_matrix(SUPPORT_MATRIX_PATH)
    except (OSError, ValueError) as error:
        return [f"SUPPORT_MATRIX_INVALID:{type(error).__name__}"]
    violations: list[str] = []
    if len(matrix.features) != 11:
        violations.append("SUPPORT_FEATURE_COUNT_INVALID")
    for feature in matrix.features:
        if not feature.detector or not feature.validator or not feature.fixture_id:
            violations.append(f"SUPPORT_EVIDENCE_MISSING:{feature.name}")
    config = CONFIG_PATH.read_text(encoding="utf-8")
    if "preservation_support_matrix" not in config:
        violations.append("SUPPORT_MATRIX_NOT_IN_CENTRAL_CONFIG")
    return sorted(violations)


def forbidden_api_violations(root: Path = KERNEL_ROOT) -> list[str]:
    """扫描生产 Kernel，禁止浏览器、HTML、系统字体和页级 PDF 合并原语。"""

    patterns = {
        "HTML_RENDERER": re.compile(r"insert_htmlbox", re.IGNORECASE),
        "BROWSER_RENDERER": re.compile(r"chrom(?:e|ium)", re.IGNORECASE),
        "SYSTEM_FONT_PATH": re.compile(r"windows[/\\]fonts", re.IGNORECASE),
        "PAGE_PDF_MERGE": re.compile(r"show_pdf_page|insert_pdf", re.IGNORECASE),
    }
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        for code, pattern in patterns.items():
            if pattern.search(content):
                violations.append(f"{code}:{path.relative_to(root).as_posix()}")
    return violations


def semantic_boundary_violations() -> list[str]:
    """确认机械内核不导入分类、Toolbox，也不包含已知页面 Route 分支。"""

    violations: list[str] = []
    forbidden = (
        "transflow.classification",
        "transflow.toolboxes",
        "body.flow_text",
        "visual_only",
    )
    for path in sorted(KERNEL_ROOT.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in content:
                violations.append(
                    f"KERNEL_PAGE_SEMANTIC:{path.relative_to(REPO_ROOT).as_posix()}:{token}"
                )
    return violations


def determinism_violations() -> list[str]:
    """核对批准角色、冻结容差和统一 Kernel 指纹均完整可复算。"""

    contract = json.loads(DETERMINISM_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(RUNTIME_BASELINE_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    if contract.get("schema_version") != "transflow.p6-determinism/v1":
        violations.append("DETERMINISM_SCHEMA_INVALID")
    roles = contract.get("approved_roles")
    if roles != ["development", "target"]:
        violations.append("APPROVED_ROLES_INVALID")
    baseline_roles = baseline.get("environment_roles", {})
    if any(role not in baseline_roles for role in roles or ()):
        violations.append("RUNTIME_ROLE_NOT_APPROVED")
    tolerances = contract.get("tolerances")
    if not isinstance(tolerances, dict) or any(
        not isinstance(value, int | float) or float(value) != 0.0
        for value in tolerances.values()
    ):
        violations.append("DETERMINISM_TOLERANCE_NOT_ZERO")
    fingerprint = build_kernel_fingerprint(FONT_MANIFEST_PATH, SUPPORT_MATRIX_PATH)
    if len(fingerprint.fingerprint) != 64:
        violations.append("KERNEL_FINGERPRINT_INVALID")
    return violations


def test_inventory_violations() -> list[str]:
    """用 AST 核对 P6.1～P6.5 每个 T01～T06 恰有一个真实测试函数。"""

    tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"), filename=str(TEST_PATH))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_p6_")
    }
    violations: list[str] = []
    for stage in range(1, 6):
        for case in range(1, 7):
            prefix = f"test_p6_{stage}_t{case:02d}_"
            count = sum(name.startswith(prefix) for name in names)
            if count != 1:
                violations.append(f"TEST_ID_COUNT:{stage}.T{case:02d}:{count}")
    if len(names) != 30:
        violations.append(f"P6_TEST_TOTAL:{len(names)}")
    return violations


def all_checks() -> dict[str, list[str]]:
    """返回 G6 可静态复算的全部检查及逐项违规。"""

    return {
        "migration": migration_violations(),
        "support_matrix": support_matrix_violations(),
        "forbidden_api": forbidden_api_violations(),
        "semantic_boundary": semantic_boundary_violations(),
        "determinism": determinism_violations(),
        "test_inventory": test_inventory_violations(),
    }


CHECKS: dict[str, Callable[[], list[str]]] = {
    "migration": migration_violations,
    "support_matrix": support_matrix_violations,
    "forbidden_api": forbidden_api_violations,
    "semantic_boundary": semantic_boundary_violations,
    "determinism": determinism_violations,
    "test_inventory": test_inventory_violations,
}


def parse_args() -> argparse.Namespace:
    """解析 P6 审计范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P6 SharedPdfKernel")
    parser.add_argument("check", choices=("all", *CHECKS))
    return parser.parse_args()


def main() -> int:
    """执行选定审计，并输出可直接保存到 Gate 报告的稳定文本。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    selected = parse_args().check
    results = all_checks() if selected == "all" else {selected: CHECKS[selected]()}
    for name, violations in results.items():
        status = "PASS" if not violations else "FAIL"
        print(f"P6_VERIFY check={name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P6_VIOLATION check={name} detail={violation}")
    passed = not any(results.values())
    print(f"P6_VERIFY_SUMMARY status={'PASS' if passed else 'FAIL'} checks={len(results)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
