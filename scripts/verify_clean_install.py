"""在两个真实空 venv 中验证 Transflow 锁文件可复现安装与 smoke。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.p1.clean-install")
REPO_ROOT = Path(__file__).resolve().parent.parent
CLEAN_ROOT = REPO_ROOT / "tmp" / "p1-clean-installs"
LOCK_PATH = REPO_ROOT / "requirements.lock"
PIP_VERSION = "26.1.2"


def configure_logging() -> None:
    """配置干净安装验证日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def _validate_clean_root(path: Path) -> Path:
    """确认清理目标严格位于专用 tmp 目录，避免误删用户文件。"""

    resolved = path.resolve()
    expected_parent = (REPO_ROOT / "tmp").resolve()
    if resolved.parent != expected_parent or resolved.name != "p1-clean-installs":
        raise ValueError("干净安装目录不符合 P1 专用边界")
    return resolved


def _python_path(environment: Path) -> Path:
    """返回当前冻结 Windows 目标环境中的 venv Python 路径。"""

    return environment / "Scripts" / "python.exe"


def _run(command: list[str], intent: str) -> subprocess.CompletedProcess[str]:
    """执行安装或 smoke 命令并在失败时保留完整真实输出。"""

    LOGGER.info("调用子进程，意图=%s command=%s", intent, subprocess.list2cmdline(command))
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"命令失败 intent={intent} code={completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def _build_wheel(output_directory: Path) -> Path:
    """从当前源码构建真实 wheel，供两个空环境安装相同制品。"""

    output_directory.mkdir(parents=True, exist_ok=True)
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_directory),
        ],
        "构建 P1 干净安装 wheel",
    )
    wheels = sorted(output_directory.glob("transflow_pdf_engine-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"预期唯一 wheel，实际数量={len(wheels)}")
    return wheels[0]


def _install_one(environment: Path, wheel_path: Path, label: str) -> dict[str, Any]:
    """创建一个空 venv，按唯一锁安装并执行依赖检查和真实 smoke。"""

    LOGGER.info("创建空 venv，意图=验证可复现安装 label=%s", label)
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = _python_path(environment)
    _run(
        [str(python), "-m", "pip", "install", "--progress-bar", "off", f"pip=={PIP_VERSION}"],
        f"{label} 固定 pip",
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--progress-bar",
            "off",
            "--no-deps",
            "-r",
            str(LOCK_PATH),
        ],
        f"{label} 按唯一锁安装",
    )
    _run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
        f"{label} 安装 Transflow wheel",
    )
    check = _run([str(python), "-m", "pip", "check"], f"{label} 检查依赖一致性")
    packages = json.loads(
        _run(
            [str(python), "-m", "pip", "list", "--format=json"],
            f"{label} 导出版本清单",
        ).stdout
    )
    smoke_code = (
        "import json,sys;"
        "from pathlib import Path;"
        "from transflow.runtime.config import load_runtime_config;"
        "from transflow.runtime.probes import create_and_reopen_minimal_pdf;"
        "root=Path(sys.argv[1]);"
        "config=load_runtime_config(root/'config'/'transflow.example.toml');"
        "result=create_and_reopen_minimal_pdf(config.workspace/'clean-install-smoke');"
        "assert result['page_count']==1;"
        "print(json.dumps({'import':'transflow','pdf':result,'config':config.schema_version}))"
    )
    smoke = _run(
        [str(python), "-I", "-c", smoke_code, str(REPO_ROOT)],
        f"{label} 清除 PYTHONPATH 后运行 smoke",
    )
    return {
        "label": label,
        "python": _run([str(python), "--version"], f"{label} 读取 Python 版本").stdout.strip(),
        "pip_check": check.stdout.strip(),
        "packages": sorted(packages, key=lambda item: str(item["name"]).casefold()),
        "smoke": json.loads(smoke.stdout),
    }


def verify_two_clean_installs() -> dict[str, Any]:
    """重建两个空 venv，比较依赖清单并汇总真实 smoke 结果。"""

    clean_root = _validate_clean_root(CLEAN_ROOT)
    if clean_root.exists():
        shutil.rmtree(clean_root)
    clean_root.mkdir(parents=True)
    wheel = _build_wheel(clean_root / "dist")
    first = _install_one(clean_root / "venv-one", wheel, "venv-one")
    second = _install_one(clean_root / "venv-two", wheel, "venv-two")
    package_diff = first["packages"] != second["packages"]
    return {
        "schema_version": "transflow.clean-install-evidence/v1",
        "wheel": wheel.name,
        "first": first,
        "second": second,
        "package_diff_count": 1 if package_diff else 0,
        "pip_check_error_count": sum(
            0 if item["pip_check"] == "No broken requirements found." else 1
            for item in (first, second)
        ),
        "smoke_success_count": sum(
            1 if item["smoke"]["pdf"]["page_count"] == 1 else 0 for item in (first, second)
        ),
    }


def main() -> int:
    """执行两次干净安装并输出可直接进入报告的 JSON 证据。"""

    configure_logging()
    evidence = verify_two_clean_installs()
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (
        evidence["package_diff_count"] == 0
        and evidence["pip_check_error_count"] == 0
        and evidence["smoke_success_count"] == 2
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
