"""执行 Transflow P0 的目录、边界、追溯、决策、排期和 wheel 核验。"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from scripts import build_p0_assets as assets

LOGGER = logging.getLogger("transflow.p0.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
GOVERNANCE_PATH = REPO_ROOT / "docs" / "迁移" / "governance_registry.json"
SCHEDULE_PATH = REPO_ROOT / "docs" / "计划" / "Transflow_P1-P14_依赖迭代排期_v0.1.json"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def configure_logging() -> None:
    """配置 P0 核验脚本日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def load_json(path: Path) -> dict[str, Any]:
    """读取仓库内 UTF-8 JSON 配置。"""

    return json.loads(path.read_text(encoding="utf-8"))


def is_within_repo(path: Path) -> bool:
    """判断解析后的路径是否仍位于 Transflow 仓库。"""

    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def check_baseline() -> list[str]:
    """核验 P0 生成资产与当前设计和迁移来源完全一致。"""

    LOGGER.info("核验 P0 可重算基线")
    return assets.check_assets()


def production_python_files() -> list[Path]:
    """枚举新生产包中的 Python 文件。"""

    return sorted((REPO_ROOT / "src" / "transflow").rglob("*.py"))


def check_package_boundary() -> list[str]:
    """核验一级施工目录、包来源、导入方向和空壳类。"""

    LOGGER.info("核验生产目录和包边界")
    violations: list[str] = []
    required_paths = [
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
        REPO_ROOT / "docs" / "合同",
        REPO_ROOT / "docs" / "迁移",
        REPO_ROOT / "docs" / "reports" / "gates",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "resources" / "manifests",
        REPO_ROOT / "scripts",
        REPO_ROOT / "src" / "transflow",
        REPO_ROOT / "tests",
    ]
    for path in required_paths:
        if not path.exists():
            violations.append(f"MISSING_PATH:{path.relative_to(REPO_ROOT).as_posix()}")
    spec = importlib.util.find_spec("transflow")
    if spec is None or spec.origin is None:
        violations.append("TRANSFLOW_IMPORT_NOT_FOUND")
    else:
        origin = Path(spec.origin).resolve()
        expected_root = (REPO_ROOT / "src" / "transflow").resolve()
        if expected_root not in (origin, *origin.parents):
            violations.append(f"TRANSFLOW_IMPORT_OUTSIDE_SRC:{origin}")
    forbidden_roots = {"MerqFin", "backend", "spikes"}
    # 盘符前不能紧邻标识符字符，避免把 ``http://`` 中的 ``p:/`` 误判为 Windows 路径。
    drive_pattern = re.compile(r"(?i)(?<![a-z0-9_])[a-z]:[\\/]")
    for path in production_python_files():
        source = path.read_text(encoding="utf-8")
        if drive_pattern.search(source):
            violations.append(f"ABSOLUTE_DRIVE_PATH:{path.relative_to(REPO_ROOT).as_posix()}")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            imported_root: str | None = None
            if isinstance(node, ast.Import) and node.names:
                imported_root = node.names[0].name.split(".", maxsplit=1)[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_root = node.module.split(".", maxsplit=1)[0]
            if imported_root in forbidden_roots:
                violations.append(
                    f"FORBIDDEN_IMPORT:{path.relative_to(REPO_ROOT).as_posix()}:{imported_root}"
                )
        for node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
            # 仅承担稳定异常类型身份的 marker class 是完整实现，不属于未施工业务空壳。
            exception_marker = any(
                isinstance(base, ast.Name)
                and base.id in {"BaseException", "Exception", "RuntimeError", "ValueError"}
                for base in node.bases
            )
            meaningful = [
                item
                for item in node.body
                if not isinstance(item, ast.Pass)
                and not (
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                )
            ]
            if not meaningful and not exception_marker:
                violations.append(
                    f"EMPTY_CLASS:{path.relative_to(REPO_ROOT).as_posix()}:{node.name}"
                )
    return sorted(set(violations))


def audit_wheel(wheel_path: Path) -> list[str]:
    """读取真实 wheel 目录并返回禁止材料命中。"""

    LOGGER.info("审计 wheel 内容 path=%s", wheel_path.name)
    if not is_within_repo(wheel_path):
        return ["WHEEL_OUTSIDE_REPOSITORY"]
    if not wheel_path.is_file():
        return ["WHEEL_NOT_FOUND"]
    forbidden_fragments = (
        "spikes/",
        "样本/",
        "测试数据/",
        "tmp/",
        "transflow-legacy-workflows/",
        "/runs/",
        "/reports/",
    )
    violations: list[str] = []
    with zipfile.ZipFile(wheel_path) as wheel:
        for raw_name in wheel.namelist():
            normalized = raw_name.replace("\\", "/")
            lowered = normalized.casefold()
            if any(fragment.casefold() in lowered for fragment in forbidden_fragments):
                violations.append(f"FORBIDDEN_WHEEL_MEMBER:{normalized}")
    return sorted(violations)


def check_ci() -> list[str]:
    """核验快速 CI 与本地 G0 入口一致且不误跑重型 E2E。"""

    LOGGER.info("核验快速 CI 配置")
    if not CI_PATH.is_file():
        return ["CI_FILE_MISSING"]
    content = CI_PATH.read_text(encoding="utf-8")
    violations: list[str] = []
    required_tokens = ("push:", "pull_request:", "python scripts/run_gate.py G0")
    for token in required_tokens:
        if token not in content:
            violations.append(f"CI_REQUIRED_TOKEN_MISSING:{token}")
    forbidden_tokens = ("样本/年报", "-m e2e", "tests/e2e", "schedule:")
    for token in forbidden_tokens:
        if token in content:
            violations.append(f"CI_HEAVY_TRIGGER_FOUND:{token}")
    return violations


def check_traceability() -> list[str]:
    """核验任务七段式追溯生成物没有悬空设计、测试或 Gate。"""

    LOGGER.info("核验设计、任务、测试和 Gate 双向追溯")
    if not assets.TRACEABILITY_PATH.is_file():
        return ["TRACEABILITY_FILE_MISSING"]
    traceability = load_json(assets.TRACEABILITY_PATH)
    violations: list[str] = []
    validation = traceability["validation"]
    for key in (
        "invalid_design_references",
        "dangling_gate_test_references",
        "unowned_test_definitions",
    ):
        for value in validation[key]:
            violations.append(f"{key.upper()}:{value}")
    for task in traceability["tasks"]:
        task_id = str(task["task_id"])
        if not task["design_sections"]:
            violations.append(f"TASK_WITHOUT_DESIGN:{task_id}")
        if not task["delivery_contract"]:
            violations.append(f"TASK_WITHOUT_DELIVERY:{task_id}")
        if not task["test_ids"]:
            violations.append(f"TASK_WITHOUT_TEST:{task_id}")
        if not task["gate_items"]:
            violations.append(f"TASK_WITHOUT_GATE:{task_id}")
    return violations


def evaluate_gate_conclusion(open_decisions: list[str], checks_passed: bool) -> str:
    """按治理台账规则裁决 Gate，确保待决策优先阻断。"""

    if open_decisions:
        return "BLOCKED_BY_DECISION"
    if not checks_passed:
        return "FAIL"
    return "PASS"


def check_governance() -> list[str]:
    """核验决策登记、行为变化和 Gate 状态规则可执行。"""

    LOGGER.info("核验待决策和治理登记规则")
    if not GOVERNANCE_PATH.is_file():
        return ["GOVERNANCE_FILE_MISSING"]
    registry = load_json(GOVERNANCE_PATH)
    violations: list[str] = []
    allowed = set(registry["allowed_stage_statuses"])
    current = registry["current_stage"]
    if current["status"] not in allowed:
        violations.append("INVALID_CURRENT_STAGE_STATUS")
    if evaluate_gate_conclusion(["D-P0-SIMULATED"], True) != "BLOCKED_BY_DECISION":
        violations.append("OPEN_DECISION_DID_NOT_BLOCK")
    if evaluate_gate_conclusion([], False) != "FAIL":
        violations.append("FAILED_CHECK_DID_NOT_FAIL_GATE")
    if evaluate_gate_conclusion([], True) != "PASS":
        violations.append("CLEAN_GATE_DID_NOT_PASS")
    required_templates = {"behavior_change", "decision", "risk"}
    if set(registry["record_templates"]) != required_templates:
        violations.append("GOVERNANCE_TEMPLATE_SET_INCOMPLETE")
    return violations


def check_schedule() -> list[str]:
    """核验 P1-P14 单通道依赖排期和 G14 强制停点。"""

    LOGGER.info("核验 P1-P14 迭代依赖排期")
    if not SCHEDULE_PATH.is_file():
        return ["SCHEDULE_FILE_MISSING"]
    schedule = load_json(SCHEDULE_PATH)
    violations: list[str] = []
    first_part = schedule["first_part"]
    expected_stages = [f"P{number}" for number in range(1, 15)]
    actual_stages = [str(item["stage"]) for item in first_part]
    if actual_stages != expected_stages:
        violations.append("P1_P14_STAGE_SEQUENCE_INVALID")
    for expected_order, item in enumerate(first_part, start=1):
        if item["order"] != expected_order:
            violations.append(f"INVALID_ORDER:{item['stage']}")
        if item["dependency_gate"] != f"G{expected_order - 1}":
            violations.append(f"REVERSE_OR_MISSING_DEPENDENCY:{item['stage']}")
        for field in ("execution_window", "gate_window", "owner", "approver", "commit_scope"):
            if not item[field]:
                violations.append(f"SCHEDULE_FIELD_MISSING:{item['stage']}:{field}")
    if schedule["resource_assumptions"]["implementation_lanes"] != 1:
        violations.append("P0_UNAPPROVED_PARALLEL_LANES")
    if not schedule["g14_stop"]["required"]:
        violations.append("G14_STOP_NOT_REQUIRED")
    for item in schedule["second_part"]:
        if item["fixed_date"] is not None:
            violations.append(f"SECOND_PART_FIXED_DATE:{item['stage']}")
    return violations


def simulate_delay(schedule: dict[str, Any], delayed_stage: str, slots: int) -> dict[str, int]:
    """模拟 Gate 延迟，证明下游窗口只顺延而不会跳 Gate。"""

    first_part = schedule["first_part"]
    delayed_order = next(
        int(item["order"]) for item in first_part if item["stage"] == delayed_stage
    )
    return {
        str(item["stage"]): int(item["order"])
        + (slots if int(item["order"]) >= delayed_order else 0)
        for item in first_part
    }


def all_checks() -> dict[str, list[str]]:
    """运行除真实 wheel 构建外的全部 P0 快速核验。"""

    return {
        "baseline": check_baseline(),
        "package_boundary": check_package_boundary(),
        "ci": check_ci(),
        "traceability": check_traceability(),
        "governance": check_governance(),
        "schedule": check_schedule(),
    }


def parse_args() -> argparse.Namespace:
    """解析 P0 核验范围。"""

    parser = argparse.ArgumentParser(description="核验 Transflow P0 施工基线")
    parser.add_argument(
        "check",
        choices=(
            "all",
            "baseline",
            "package",
            "ci",
            "traceability",
            "governance",
            "schedule",
            "wheel",
        ),
    )
    parser.add_argument("--wheel", help="相对仓库根的 wheel 路径，仅 check=wheel 使用")
    return parser.parse_args()


def main() -> int:
    """执行选定 P0 核验并以 JSON 输出真实结果。"""

    configure_logging()
    args = parse_args()
    if args.check == "all":
        results = all_checks()
    elif args.check == "baseline":
        results = {"baseline": check_baseline()}
    elif args.check == "package":
        results = {"package_boundary": check_package_boundary()}
    elif args.check == "ci":
        results = {"ci": check_ci()}
    elif args.check == "traceability":
        results = {"traceability": check_traceability()}
    elif args.check == "governance":
        results = {"governance": check_governance()}
    elif args.check == "schedule":
        results = {"schedule": check_schedule()}
    else:
        if args.wheel is None:
            raise SystemExit("check=wheel 必须提供 --wheel")
        wheel_path = (REPO_ROOT / args.wheel).resolve()
        results = {"wheel": audit_wheel(wheel_path)}
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if any(violations for violations in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
