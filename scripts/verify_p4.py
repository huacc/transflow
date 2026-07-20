"""核验 P4 唯一 Kernel、固定路由隔离、最终化边界和真实 E2E fixture。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pymupdf

from scripts.verify_architecture import DEFAULT_SOURCE_ROOT, scan_production_tree

LOGGER = logging.getLogger("transflow.p4.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "src" / "transflow" / "pdf_kernel"
FINALIZER_PATH = REPO_ROOT / "src" / "transflow" / "application" / "document_finalizer.py"
E2E_FIXTURE_PATH = REPO_ROOT / "resources" / "manifests" / "p4_e2e_fixture.json"
REVIEW_PATH = REPO_ROOT / "docs" / "合同" / "P4最小PDF纵向闭环评审_v0.1.md"


def _sha256_file(path: Path) -> str:
    """流式计算完整年报 SHA-256，避免一次性加载大文件。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _production_files() -> tuple[Path, ...]:
    """稳定枚举 Transflow production wheel 对应的全部 Python 源码。"""

    return tuple(sorted(DEFAULT_SOURCE_ROOT.rglob("*.py")))


def kernel_boundary_violations() -> list[str]:
    """确认唯一解释器和 PDF 原语只位于最终 Kernel，且没有禁止实现。"""

    violations: list[str] = []
    if not KERNEL_ROOT.is_dir():
        return ["FINAL_KERNEL_MISSING"]
    interpreter_definitions: list[str] = []
    forbidden_text = {
        "TEMPORARY_KERNEL": re.compile(r"temporary[_-]?kernel", re.IGNORECASE),
        "HTML_INSERTION": re.compile(r"insert_htmlbox", re.IGNORECASE),
        "PAGE_PDF_MERGE": re.compile(r"show_pdf_page|insert_pdf|merge_page_pdf", re.IGNORECASE),
        "BROWSER_RENDERER": re.compile(r"chrom(?:e|ium)", re.IGNORECASE),
        "SYSTEM_FONT_PATH": re.compile(r"windows[/\\]fonts", re.IGNORECASE),
    }
    for path in _production_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PagePatchInterpreter":
                interpreter_definitions.append(f"{relative}:{node.lineno}")
        for code, pattern in forbidden_text.items():
            if pattern.search(source):
                violations.append(f"{code}:{relative}")
    if len(interpreter_definitions) != 1:
        violations.append(f"INTERPRETER_DEFINITION_COUNT:{len(interpreter_definitions)}")
    if interpreter_definitions and not interpreter_definitions[0].startswith(
        "src/transflow/pdf_kernel/"
    ):
        violations.append(f"INTERPRETER_OUTSIDE_KERNEL:{interpreter_definitions[0]}")
    return sorted(set(violations))


def production_route_violations() -> list[str]:
    """确认测试固定 Route 名称、映射和 fixture 不可从 production wiring 到达。"""

    violations: list[str] = []
    forbidden = ("FixedRouteFixture", "routes_by_page_identity", "p4_e2e_fixture")
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(REPO_ROOT).as_posix()
        for token in forbidden:
            if token in source:
                violations.append(f"FIXED_ROUTE_LEAK:{relative}:{token}")
    return sorted(violations)


def finalizer_boundary_violations() -> list[str]:
    """确认最终化不直接调用 PDF 页面构建、页级合并或浏览器渲染。"""

    if not FINALIZER_PATH.is_file():
        return ["FINALIZER_MISSING"]
    source = FINALIZER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(FINALIZER_PATH))
    forbidden_calls = {"insert_pdf", "new_page", "show_pdf_page"}
    violations = [
        f"FORBIDDEN_FINALIZER_CALL:{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
    ]
    if re.search(r"chrom(?:e|ium)|insert_htmlbox", source, re.IGNORECASE):
        violations.append("FORBIDDEN_FINALIZER_RENDERER")
    return sorted(violations)


def fixture_violations() -> list[str]:
    """核对登记的 F2 完整年报真实存在、哈希和页数没有漂移。"""

    if not E2E_FIXTURE_PATH.is_file():
        return ["P4_E2E_FIXTURE_MISSING"]
    payload = json.loads(E2E_FIXTURE_PATH.read_text(encoding="utf-8"))
    source = (REPO_ROOT / str(payload["relative_source"])).resolve()
    try:
        source.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return ["P4_E2E_FIXTURE_OUTSIDE_REPO"]
    violations: list[str] = []
    if not source.is_file():
        return ["P4_E2E_SOURCE_MISSING"]
    if _sha256_file(source) != payload.get("source_sha256"):
        violations.append("P4_E2E_SOURCE_HASH_DRIFT")
    try:
        with pymupdf.open(source) as document:
            if document.page_count != payload.get("page_count"):
                violations.append("P4_E2E_PAGE_COUNT_DRIFT")
    except Exception as error:
        violations.append(f"P4_E2E_SOURCE_UNREADABLE:{type(error).__name__}")
    if payload.get("fixture_tier") != "F2":
        violations.append("P4_E2E_FIXTURE_NOT_F2")
    return violations


def architecture_violations() -> list[str]:
    """复用 P2 架构扫描，确认 P4 新模块没有反转依赖或引入禁用组件。"""

    return [
        f"{item.code}:{item.relative_path}:{item.line}"
        for item in scan_production_tree(DEFAULT_SOURCE_ROOT)
    ]


def review_violations() -> list[str]:
    """确认 P4 冻结边界评审已通过且没有未关闭问题。"""

    if not REVIEW_PATH.is_file():
        return ["P4_REVIEW_MISSING"]
    content = REVIEW_PATH.read_text(encoding="utf-8")
    violations: list[str] = []
    if "评审结论：PASS" not in content:
        violations.append("P4_REVIEW_NOT_PASS")
    if "开放问题：0" not in content:
        violations.append("P4_REVIEW_OPEN_ISSUES")
    return violations


def all_checks() -> dict[str, list[str]]:
    """运行 P4 可静态复算的全部边界和 fixture 检查。"""

    return {
        "kernel": kernel_boundary_violations(),
        "routes": production_route_violations(),
        "finalizer": finalizer_boundary_violations(),
        "fixture": fixture_violations(),
        "architecture": architecture_violations(),
        "review": review_violations(),
    }


CHECKS: dict[str, Callable[[], list[str]]] = {
    "kernel": kernel_boundary_violations,
    "routes": production_route_violations,
    "finalizer": finalizer_boundary_violations,
    "fixture": fixture_violations,
    "architecture": architecture_violations,
    "review": review_violations,
}


def parse_args() -> argparse.Namespace:
    """解析 P4 检查范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P4 纵向闭环边界")
    parser.add_argument("check", choices=("all", *CHECKS))
    return parser.parse_args()


def main() -> int:
    """执行选定检查并输出可被 Gate 直接保存的逐项结果。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    selected = parse_args().check
    results = all_checks() if selected == "all" else {selected: CHECKS[selected]()}
    for name, violations in results.items():
        status = "PASS" if not violations else "FAIL"
        print(f"P4_VERIFY check={name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P4_VIOLATION check={name} detail={violation}")
    passed = not any(results.values())
    print(f"P4_VERIFY_SUMMARY status={'PASS' if passed else 'FAIL'} checks={len(results)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
