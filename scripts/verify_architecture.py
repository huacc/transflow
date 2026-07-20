"""执行 P2 架构分层、生产引用红线和可注入突变检查。"""

from __future__ import annotations

import argparse
import ast
import logging
import re
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("transflow.p2.architecture")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_ROOT = REPO_ROOT / "src" / "transflow"
FORBIDDEN_TEXT_PATTERNS = {
    "SPIKE_REFERENCE": re.compile(r"\bspikes?\b", re.IGNORECASE),
    "HOST_REFERENCE": re.compile(r"\bmerqfin\b", re.IGNORECASE),
    "LITELLM_REFERENCE": re.compile(r"\blitellm\b", re.IGNORECASE),
    "MODEL_ENDPOINT": re.compile(r"https?://[A-Za-z0-9]|model[_-]?endpoint", re.IGNORECASE),
    "AGENT_FRAMEWORK": re.compile(r"\b(?:langgraph|autogen|crewai)\b", re.IGNORECASE),
    "CHROME_REFERENCE": re.compile(r"\bchrome\b", re.IGNORECASE),
    "HTML_INSERTION": re.compile(r"insert_htmlbox", re.IGNORECASE),
    "PAGE_PDF_MERGE": re.compile(
        r"page[_-]?pdf[_-]?(?:merge|stitch)|merge_page_pdf",
        re.IGNORECASE,
    ),
}
STDLIB_ALLOWED_PREFIXES = {
    "__future__",
    "abc",
    "argparse",
    "ast",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "logging",
    "pathlib",
    "re",
    "typing",
}


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    """表示可定位到文件和行号的一条架构规则违规。"""

    code: str
    relative_path: str
    line: int
    detail: str


def layer_for(path: Path, source_root: Path) -> str | None:
    """根据生产包相对路径识别受控架构层。"""

    relative = path.relative_to(source_root)
    return relative.parts[0] if len(relative.parts) > 1 else None


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """从语法树提取显式 import 模块及其行号。"""

    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno))
    return imports


def import_allowed(layer: str | None, module: str) -> bool:
    """判断一个 import 是否符合 P2 单向依赖边界。"""

    root_name = module.split(".", maxsplit=1)[0]
    if root_name in STDLIB_ALLOWED_PREFIXES:
        return True
    if layer == "domain":
        return module == "transflow.domain" or module.startswith("transflow.domain.")
    if layer == "ports":
        return module == "transflow.domain" or module.startswith(
            ("transflow.domain.", "transflow.ports.")
        )
    if not module.startswith("transflow."):
        return True
    imported_layer = module.split(".")[1]
    allowed_layers = {
        "application": {
            "application",
            "classification",
            "core",
            "domain",
            "pdf_kernel",
            "ports",
            "toolboxes",
        },
        "adapters": {"adapters", "core", "domain", "pdf_kernel", "ports"},
        "runtime": {
            "adapters",
            "application",
            "core",
            "domain",
            "pdf_kernel",
            "ports",
            "runtime",
            "toolboxes",
        },
        "core": {"core", "domain"},
        "pdf_kernel": {"domain", "pdf_kernel"},
        "classification": {"classification", "domain", "pdf_kernel", "ports"},
        "toolboxes": {"domain", "pdf_kernel", "toolboxes"},
    }
    return layer not in allowed_layers or imported_layer in allowed_layers[layer]


def scan_production_tree(source_root: Path) -> list[ArchitectureViolation]:
    """扫描生产 Python 文件并返回稳定排序的全部违规。"""

    violations: list[ArchitectureViolation] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as error:
            violations.append(
                ArchitectureViolation("SYNTAX_ERROR", relative, error.lineno or 0, str(error))
            )
            continue
        layer = layer_for(path, source_root)
        for module, line in imported_modules(tree):
            if not import_allowed(layer, module):
                violations.append(
                    ArchitectureViolation(
                        "ILLEGAL_LAYER_IMPORT",
                        relative,
                        line,
                        f"layer={layer} module={module}",
                    )
                )
        for code, pattern in FORBIDDEN_TEXT_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(ArchitectureViolation(code, relative, line, match.group(0)))
        if layer == "domain":
            for module, line in imported_modules(tree):
                if module.split(".")[0] not in STDLIB_ALLOWED_PREFIXES | {"transflow"}:
                    violations.append(
                        ArchitectureViolation(
                            "DOMAIN_EXTERNAL_DEPENDENCY",
                            relative,
                            line,
                            module,
                        )
                    )
    return sorted(violations, key=lambda item: (item.relative_path, item.line, item.code))


def resolve_source_root(relative_path: str | None) -> Path:
    """解析仓库内相对扫描路径并拒绝目录逃逸。"""

    if relative_path is None:
        return DEFAULT_SOURCE_ROOT
    requested = Path(relative_path)
    if requested.is_absolute():
        raise ValueError("--root 必须是仓库相对路径")
    resolved = (REPO_ROOT / requested).resolve()
    resolved.relative_to(REPO_ROOT.resolve())
    return resolved


def parse_args() -> argparse.Namespace:
    """解析可选的仓库相对生产源码根路径。"""

    parser = argparse.ArgumentParser(description="检查 Transflow P2 架构边界")
    parser.add_argument("--root", help="仓库相对扫描根，默认 src/transflow")
    return parser.parse_args()


def main() -> int:
    """扫描架构边界并以非零退出码报告真实违规。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    source_root = resolve_source_root(parse_args().root)
    LOGGER.info("调用架构扫描，意图=阻止跨层和禁用实现进入生产包 root=%s", source_root)
    violations = scan_production_tree(source_root)
    for violation in violations:
        print(
            "ARCHITECTURE_VIOLATION "
            f"code={violation.code} path={violation.relative_path} "
            f"line={violation.line} detail={violation.detail}"
        )
    status = "PASS" if not violations else "FAIL"
    print(f"ARCHITECTURE_SCAN {status} violations={len(violations)}")
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
