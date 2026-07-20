"""执行 Transflow P1 运行基线、依赖、配置和主机能力静态核验。"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pymupdf

from transflow.runtime.config import find_plaintext_secrets, load_runtime_config
from transflow.runtime.probes import (
    collect_environment_snapshot,
    create_and_reopen_minimal_pdf,
    render_registered_font,
    sha256_file,
    validate_font_manifest,
)

LOGGER = logging.getLogger("transflow.p1.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "resources" / "manifests" / "runtime_baseline.json"
FONT_MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
LOCK_PATH = REPO_ROOT / "requirements.lock"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CONFIG_PATH = REPO_ROOT / "config" / "transflow.example.toml"
FORBIDDEN_PRODUCTION_PACKAGES = {
    "anthropic",
    "autogen",
    "boto3",
    "botocore",
    "crewai",
    "dashscope",
    "google-generativeai",
    "google-genai",
    "langchain",
    "langgraph",
    "litellm",
    "openai",
    "pdfkit",
    "playwright",
    "pyppeteer",
    "selenium",
    "weasyprint",
}


def configure_logging() -> None:
    """配置 P1 核验日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def load_json(path: Path) -> dict[str, Any]:
    """读取仓库内 UTF-8 JSON 清单。"""

    return json.loads(path.read_text(encoding="utf-8"))


def check_runtime_baseline() -> list[str]:
    """核对解释器、PyMuPDF、字体版本/哈希和环境角色字段。"""

    LOGGER.info("调用运行基线核验，意图=阻止解释器、PDF 引擎和字体漂移")
    baseline = load_json(BASELINE_PATH)
    violations: list[str] = []
    if baseline.get("schema_version") != "transflow.runtime-baseline/v1":
        violations.append("RUNTIME_BASELINE_SCHEMA_INVALID")
        return violations
    if platform.python_version() != baseline["python"]["version"]:
        violations.append("PYTHON_VERSION_MISMATCH")
    base_executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if sha256_file(base_executable) != baseline["python"]["executable_sha256"]:
        violations.append("PYTHON_EXECUTABLE_SHA256_MISMATCH")
    if pymupdf.__version__ != baseline["pymupdf"]["version"]:
        violations.append("PYMUPDF_VERSION_MISMATCH")
    module_path = Path(pymupdf.__file__).resolve()
    if sha256_file(module_path) != baseline["pymupdf"]["module_sha256"]:
        violations.append("PYMUPDF_MODULE_SHA256_MISMATCH")
    roles = baseline.get("environment_roles", {})
    if not roles.get("development") or not roles.get("target"):
        violations.append("ENVIRONMENT_ROLE_MISSING")
    font_findings = validate_font_manifest(FONT_MANIFEST_PATH)
    violations.extend(item.code for item in font_findings if not item.passed)
    return sorted(set(violations))


def open_license_decisions() -> list[str]:
    """返回仍阻断 G1 的许可决策，不把法律选择伪装成技术通过。"""

    baseline = load_json(BASELINE_PATH)
    return [str(item) for item in baseline.get("unresolved_license_items", [])]


def _normalized_dependency_name(requirement: str) -> str:
    """从精确或范围依赖表达式中提取规范化包名。"""

    raw_name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0]
    return raw_name.strip().lower().replace("_", "-")


def find_forbidden_dependencies(requirements: list[str]) -> list[str]:
    """返回 production 依赖中的禁止包名，供真实 Gate 与注入测试共用。"""

    return sorted(
        name
        for name in map(_normalized_dependency_name, requirements)
        if name in FORBIDDEN_PRODUCTION_PACKAGES
    )


def check_dependencies() -> list[str]:
    """核验单一精确锁、production 禁止项、路径依赖和当前环境一致性。"""

    LOGGER.info("调用依赖核验，意图=阻止未锁版本、禁止包和跨仓库路径")
    violations: list[str] = []
    lock_lines = [
        line.strip()
        for line in LOCK_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in lock_lines:
        if line.count("==") != 1 or any(token in line for token in (" @ ", "../", "..\\")):
            violations.append(f"LOCK_ENTRY_NOT_EXACT:{line}")
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    production = [str(item) for item in project["project"]["dependencies"]]
    forbidden = find_forbidden_dependencies(production)
    violations.extend(f"FORBIDDEN_PRODUCTION_DEPENDENCY:{name}" for name in forbidden)
    if len(list(REPO_ROOT.glob("*requirements*.lock"))) != 1:
        violations.append("LOCK_FILE_COUNT_NOT_ONE")
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        violations.append(f"PIP_CHECK_FAILED:{completed.stdout.strip()}:{completed.stderr.strip()}")
    return sorted(set(violations))


def check_configuration() -> list[str]:
    """核验配置能统一读取且提交文件与日志样本没有秘密明文。"""

    LOGGER.info("调用配置核验，意图=验证无秘密模板与统一读取入口")
    violations: list[str] = []
    load_runtime_config(CONFIG_PATH)
    scan_paths = [CONFIG_PATH]
    scan_paths.extend(sorted((REPO_ROOT / "tmp").glob("p1-*.log")))
    for path in scan_paths:
        hits = find_plaintext_secrets(path.read_text(encoding="utf-8", errors="replace"))
        violations.extend(f"PLAINTEXT_SECRET:{path.name}:{hit}" for hit in hits)
    return sorted(set(violations))


def check_host_smoke() -> list[str]:
    """执行最小 PDF、字体渲染和开发/目标角色环境探针。"""

    LOGGER.info("调用主机 smoke，意图=验证后续 PDF 开发硬条件")
    violations: list[str] = []
    workspace = REPO_ROOT / "tmp" / "p1-host-smoke"
    development = collect_environment_snapshot("development")
    target = collect_environment_snapshot("target")
    comparable_keys = set(development) - {"role"}
    if not comparable_keys or comparable_keys != set(target) - {"role"}:
        violations.append("ENVIRONMENT_SNAPSHOT_NOT_COMPARABLE")
    minimal = create_and_reopen_minimal_pdf(workspace)
    if minimal["page_count"] != 1:
        violations.append("MINIMAL_PDF_OPEN_SAVE_FAILED")
    font_manifest = load_json(FONT_MANIFEST_PATH)
    font_path = REPO_ROOT / str(font_manifest["assets"][0]["path"])
    rendered = render_registered_font(workspace, font_path)
    if not rendered["passed"]:
        violations.append("FONT_RENDER_OR_PNG_FAILED")
    return violations


def all_checks() -> dict[str, list[str]]:
    """执行 P1 全部非重型静态与 smoke 核验。"""

    return {
        "runtime_baseline": check_runtime_baseline(),
        "dependencies": check_dependencies(),
        "configuration": check_configuration(),
        "host_smoke": check_host_smoke(),
        "open_license_decisions": open_license_decisions(),
    }


def parse_args() -> argparse.Namespace:
    """解析 P1 核验范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P1 环境与服务基线")
    parser.add_argument(
        "check",
        choices=("all", "baseline", "dependencies", "configuration", "host"),
    )
    return parser.parse_args()


def main() -> int:
    """执行选定核验，输出真实 JSON，并对未决许可返回阻断退出码 2。"""

    configure_logging()
    args = parse_args()
    if args.check == "all":
        results = all_checks()
    elif args.check == "baseline":
        results = {
            "runtime_baseline": check_runtime_baseline(),
            "open_license_decisions": open_license_decisions(),
        }
    elif args.check == "dependencies":
        results = {"dependencies": check_dependencies()}
    elif args.check == "configuration":
        results = {"configuration": check_configuration()}
    else:
        results = {"host_smoke": check_host_smoke()}
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    non_decision_failures = any(
        values for key, values in results.items() if key != "open_license_decisions"
    )
    if non_decision_failures:
        return 1
    if results.get("open_license_decisions"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
