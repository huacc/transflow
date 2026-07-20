"""按显式 JSON 清单顺序执行 Transflow 本地 Gate。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.gate")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = Path("resources/manifests/gate_catalog.json")


def configure_logging() -> None:
    """配置 Gate 编排日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def resolve_repository_path(relative_path: str | Path) -> Path:
    """解析仓库相对路径并拒绝绝对路径和目录逃逸。"""

    requested = Path(relative_path)
    if requested.is_absolute():
        raise ValueError(f"Gate 路径必须相对仓库根: {relative_path}")
    resolved = (REPO_ROOT / requested).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as error:
        raise ValueError(f"Gate 路径越出仓库根: {relative_path}") from error
    return resolved


def load_catalog(catalog_path: Path) -> dict[str, Any]:
    """读取并做最小结构校验的 Gate 清单。"""

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.gate-catalog/v1":
        raise ValueError("Gate 清单 schema_version 不受支持")
    if not isinstance(payload.get("gates"), dict):
        raise ValueError("Gate 清单缺少 gates 对象")
    return payload


def materialize_command(command: list[str]) -> list[str]:
    """把清单中的解释器占位符替换为当前真实 Python。"""

    return [sys.executable if item == "{python}" else item for item in command]


def execute_gate(
    gate_id: str,
    catalog_path: Path,
    report_path: Path,
) -> tuple[int, dict[str, Any]]:
    """顺序执行 Gate 命令，首个失败立即阻断并保留原始输出。"""

    catalog = load_catalog(catalog_path)
    raw_steps = catalog["gates"].get(gate_id)
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(f"Gate 未登记或没有步骤: {gate_id}")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    gate_started = time.perf_counter()
    results: list[dict[str, Any]] = []
    final_code = 0
    child_environment = os.environ.copy()
    # Windows 默认控制台编码可能与 UTF-8 证据格式不同；统一子进程编码，避免日志损坏。
    child_environment["PYTHONUTF8"] = "1"
    child_environment["PYTHONIOENCODING"] = "utf-8"
    for raw_step in raw_steps:
        step_id = str(raw_step["id"])
        intent = str(raw_step["intent"])
        command = materialize_command([str(item) for item in raw_step["command"]])
        LOGGER.info("调用检查 step=%s intent=%s", step_id, intent)
        LOGGER.info("执行命令 command=%s", subprocess.list2cmdline(command))
        step_started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=child_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        duration_ms = round((time.perf_counter() - step_started) * 1000)
        if completed.stdout:
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
        if completed.stderr:
            print(
                completed.stderr,
                file=sys.stderr,
                end="" if completed.stderr.endswith("\n") else "\n",
            )
        results.append(
            {
                "step_id": step_id,
                "intent": intent,
                "command": command,
                "return_code": completed.returncode,
                "duration_ms": duration_ms,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        if completed.returncode != 0:
            final_code = completed.returncode
            LOGGER.error("Gate 步骤失败 step=%s return_code=%s", step_id, final_code)
            break
    if final_code == 0:
        conclusion = "PASS"
    elif final_code == 2:
        conclusion = "BLOCKED_BY_DECISION"
    else:
        conclusion = "FAIL"
    report = {
        "schema_version": "transflow.gate-report/v1",
        "gate": gate_id,
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_ms": round((time.perf_counter() - gate_started) * 1000),
        "catalog": catalog_path.relative_to(REPO_ROOT).as_posix(),
        "conclusion": conclusion,
        "steps": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("Gate 完成 gate=%s conclusion=%s report=%s", gate_id, conclusion, report_path)
    return final_code, report


def parse_args() -> argparse.Namespace:
    """解析 Gate ID、清单和报告相对路径。"""

    parser = argparse.ArgumentParser(description="执行 Transflow 本地 Gate")
    parser.add_argument("gate_id", help="例如 G0")
    parser.add_argument(
        "--catalog",
        default=DEFAULT_CATALOG.as_posix(),
        help="相对仓库根的 Gate 清单",
    )
    parser.add_argument("--report", help="相对仓库根的 JSON 证据路径")
    return parser.parse_args()


def main() -> int:
    """执行指定 Gate 并原样返回失败退出码。"""

    configure_logging()
    args = parse_args()
    catalog_path = resolve_repository_path(args.catalog)
    report_relative = args.report or f"tmp/gates/{args.gate_id}/report.json"
    report_path = resolve_repository_path(report_relative)
    return_code, _ = execute_gate(args.gate_id, catalog_path, report_path)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
